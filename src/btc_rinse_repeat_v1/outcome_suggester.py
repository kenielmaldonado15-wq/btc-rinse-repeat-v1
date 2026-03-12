from __future__ import annotations

import argparse
import json
from ast import literal_eval
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import StrategyConfig
from .config import load_strategy_config
from .data_feed import fetch_historical_candles
from .journal import journal_entry_payload, validate_realized_row
from .models import Candle, JournalEntry


REALIZED_PATCH_REQUIRED_FIELDS = {
    "timestamp",
    "actual_outcome",
    "actual_exit_price",
    "actual_pnl_usd",
    "actual_pnl_basis",
    "executed_notional_usd",
}
REALIZED_OUTCOMES = {"win", "loss", "breakeven"}
NON_REALIZED_FORBIDDEN_FIELDS = {"actual_exit_price", "actual_pnl_usd"}


def _derive_position_id(timestamp_iso: str, decision: str, entry: str, existing: str | None = None) -> str:
    return existing or f"{timestamp_iso}|{decision}|{entry}"


def _as_utc_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_entry_by_timestamp(journal_path: str, timestamp_iso: str) -> JournalEntry:
    p = Path(journal_path)
    if not p.exists():
        raise RuntimeError(f"Journal not found: {journal_path}")

    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if str(row.get("timestamp")) == timestamp_iso:
            return JournalEntry(**row)
    raise RuntimeError(f"No journal entry found for timestamp: {timestamp_iso}")


def _fill_price_with_gap_conservative(direction: str, outcome: str, trigger_price: float, candle: Candle) -> float:
    if outcome == "win":
        return trigger_price

    # loss path: pessimistic gap handling (worse than stop if market gaps through)
    if direction == "Paper Long":
        return min(trigger_price, candle.open)
    return max(trigger_price, candle.open)


def first_touch_exit(direction: str, stop: float, target: float, candles: list[Candle]) -> tuple[str, float, datetime] | None:
    is_long = direction == "Paper Long"
    is_short = direction == "Paper Short"
    if not (is_long or is_short):
        return None

    for c in candles:
        if is_long:
            stop_hit = c.low <= stop
            target_hit = c.high >= target
        else:
            stop_hit = c.high >= stop
            target_hit = c.low <= target

        # strict conservative tie rule
        if stop_hit and target_hit:
            return "loss", _fill_price_with_gap_conservative(direction, "loss", stop, c), c.timestamp
        if stop_hit:
            return "loss", _fill_price_with_gap_conservative(direction, "loss", stop, c), c.timestamp
        if target_hit:
            return "win", _fill_price_with_gap_conservative(direction, "win", target, c), c.timestamp

    return None


def _first_entry_touch_index(entry_price: float, candles: list[Candle]) -> int | None:
    for i, c in enumerate(candles):
        if c.low <= entry_price <= c.high:
            return i
    return None


def _first_zone_touch_index(entry_zone: tuple[float, float], candles: list[Candle]) -> int | None:
    low, high = sorted(entry_zone)
    for i, c in enumerate(candles):
        overlap = max(0.0, min(c.high, high) - max(c.low, low))
        if overlap > 0:
            return i
        if low == high and c.low <= low <= c.high:
            return i
    return None


def _entry_fill_fraction(entry_zone: tuple[float, float], candle: Candle) -> float:
    low, high = sorted(entry_zone)
    if low == high:
        return 1.0 if candle.low <= low <= candle.high else 0.0
    overlap = max(0.0, min(candle.high, high) - max(candle.low, low))
    return max(0.0, min(1.0, overlap / (high - low)))


def _gross_per_btc(direction: str, entry_price: float, exit_price: float) -> float:
    if direction == "Paper Long":
        return exit_price - entry_price
    return entry_price - exit_price


def _effective_exit_price_from_gross(direction: str, entry_price: float, gross_per_btc: float) -> float:
    if direction == "Paper Long":
        return entry_price + gross_per_btc
    return entry_price - gross_per_btc


def _simulate_partial_exits(
    direction: str,
    entry_price: float,
    stop: float,
    target: float,
    candles: list[Candle],
    executed_size_btc: float,
    partial_take_profit_fraction: float,
) -> dict:
    remaining = executed_size_btc
    realized_gross = 0.0
    target_exit_size = 0.0
    stop_exit_size = 0.0
    exit_notional = 0.0
    exit_fill_count = 0
    last_exit_time: datetime | None = None
    first_target_size = max(0.0, min(executed_size_btc, executed_size_btc * partial_take_profit_fraction))
    first_target_done = False

    for c in candles:
        if remaining <= 0:
            break

        if direction == "Paper Long":
            stop_hit = c.low <= stop
            target_hit = c.high >= target
        else:
            stop_hit = c.high >= stop
            target_hit = c.low <= target

        if stop_hit and target_hit:
            stop_price = _fill_price_with_gap_conservative(direction, "loss", stop, c)
            close_size = remaining
            realized_gross += _gross_per_btc(direction, entry_price, stop_price) * close_size
            stop_exit_size += close_size
            exit_notional += abs(stop_price * close_size)
            remaining = 0.0
            exit_fill_count += 1
            last_exit_time = c.timestamp
            break

        if stop_hit:
            stop_price = _fill_price_with_gap_conservative(direction, "loss", stop, c)
            close_size = remaining
            realized_gross += _gross_per_btc(direction, entry_price, stop_price) * close_size
            stop_exit_size += close_size
            exit_notional += abs(stop_price * close_size)
            remaining = 0.0
            exit_fill_count += 1
            last_exit_time = c.timestamp
            break

        if target_hit:
            target_price = _fill_price_with_gap_conservative(direction, "win", target, c)
            if not first_target_done and first_target_size > 0 and remaining - first_target_size > 1e-12:
                close_size = first_target_size
                first_target_done = True
            else:
                close_size = remaining

            realized_gross += _gross_per_btc(direction, entry_price, target_price) * close_size
            target_exit_size += close_size
            exit_notional += abs(target_price * close_size)
            remaining = max(0.0, remaining - close_size)
            exit_fill_count += 1
            last_exit_time = c.timestamp

    return {
        "remaining_size_btc": remaining,
        "target_exit_size_btc": target_exit_size,
        "stop_exit_size_btc": stop_exit_size,
        "realized_exit_size_btc": target_exit_size + stop_exit_size,
        "realized_gross_per_btc_sum": realized_gross,
        "exit_notional_usd": exit_notional,
        "exit_fill_count": exit_fill_count,
        "last_exit_time": last_exit_time,
    }


def derive_outcome_fields(
    direction: str,
    entry_price: float,
    position_size_btc: float,
    stop: float,
    target: float,
    entry_timestamp: datetime,
    future_candles: list[Candle],
    taker_fee_rate: float,
    slippage_rate: float,
    entry_zone: tuple[float, float] | None = None,
    partial_take_profit_fraction: float = 0.5,
    residual_state: dict | None = None,
) -> dict:
    residual_state = residual_state or {}
    carry_mode = bool(residual_state.get("carry_active"))

    carry_realized_gross = float(residual_state.get("realized_gross_so_far_usd") or 0.0)
    carry_target_exit_size = float(residual_state.get("target_exit_size_btc") or 0.0)
    carry_stop_exit_size = float(residual_state.get("stop_exit_size_btc") or 0.0)
    carry_realized_exit_size = float(residual_state.get("realized_exit_size_btc") or 0.0)
    carry_exit_fill_count = int(residual_state.get("exit_fill_count") or 0)
    carry_lifecycle_stage = int(residual_state.get("residual_lifecycle_stage") or 0)
    carry_entry_fill_ts = _as_utc_datetime(residual_state.get("entry_fill_timestamp"))
    carry_position_id = residual_state.get("position_id")
    carry_position_open_ts = _as_utc_datetime(residual_state.get("position_open_timestamp"))

    if carry_mode:
        entry_fill_time = carry_entry_fill_ts or entry_timestamp
        fill_fraction = 1.0
        executed_size = float(residual_state.get("executed_size_btc") or position_size_btc)
        unfilled_size = float(residual_state.get("unfilled_size_btc") or 0.0)
        sim = _simulate_partial_exits(
            direction=direction,
            entry_price=entry_price,
            stop=stop,
            target=target,
            candles=future_candles,
            executed_size_btc=float(residual_state.get("residual_position_size_btc") or 0.0),
            partial_take_profit_fraction=1.0,
        )
    else:
        if entry_zone is not None:
            entry_touch_idx = _first_zone_touch_index(entry_zone, future_candles)
        else:
            entry_touch_idx = _first_entry_touch_index(entry_price, future_candles)

        if entry_touch_idx is None:
            return {
                "actual_outcome": "open",
                "review_note": "Entry not filled in fetched window",
            }

        fill_candle = future_candles[entry_touch_idx]
        entry_fill_time = fill_candle.timestamp
        fill_fraction = _entry_fill_fraction(entry_zone, fill_candle) if entry_zone is not None else 1.0
        executed_size = max(0.0, position_size_btc * fill_fraction)
        if executed_size <= 0:
            return {
                "actual_outcome": "open",
                "review_note": "Entry not filled in fetched window",
            }

        unfilled_size = max(0.0, position_size_btc - executed_size)

        if direction == "Paper Long":
            fill_stop_hit = fill_candle.low <= stop
        else:
            fill_stop_hit = fill_candle.high >= stop

        if fill_stop_hit:
            stop_fill = _fill_price_with_gap_conservative(direction, "loss", stop, fill_candle)
            sim = {
                "remaining_size_btc": 0.0,
                "target_exit_size_btc": 0.0,
                "stop_exit_size_btc": executed_size,
                "realized_exit_size_btc": executed_size,
                "realized_gross_per_btc_sum": _gross_per_btc(direction, entry_price, stop_fill) * executed_size,
                "exit_notional_usd": abs(stop_fill * executed_size),
                "exit_fill_count": 1,
                "last_exit_time": fill_candle.timestamp,
            }
        else:
            sim = _simulate_partial_exits(
                direction=direction,
                entry_price=entry_price,
                stop=stop,
                target=target,
                candles=future_candles[entry_touch_idx + 1 :],
                executed_size_btc=executed_size,
                partial_take_profit_fraction=partial_take_profit_fraction,
            )

    remaining_size = float(sim["remaining_size_btc"])
    target_exit_size = carry_target_exit_size + float(sim["target_exit_size_btc"])
    stop_exit_size = carry_stop_exit_size + float(sim["stop_exit_size_btc"])
    realized_exit_size = carry_realized_exit_size + float(sim["realized_exit_size_btc"])
    realized_gross_total = carry_realized_gross + float(sim["realized_gross_per_btc_sum"])
    lifecycle_stage = carry_lifecycle_stage + 1
    total_exit_fill_count = carry_exit_fill_count + int(sim["exit_fill_count"])
    last_event_ts = _as_utc_datetime(sim["last_exit_time"])

    base_fields = {
        "intended_position_size_btc": position_size_btc,
        "executed_size_btc": round(executed_size, 8),
        "unfilled_size_btc": round(unfilled_size, 8),
        "entry_fill_fraction": round(fill_fraction, 6),
        "target_exit_size_btc": round(target_exit_size, 8),
        "stop_exit_size_btc": round(stop_exit_size, 8),
        "residual_position_size_btc": round(remaining_size, 8),
        "realized_exit_size_btc": round(realized_exit_size, 8),
        "exit_fill_count": total_exit_fill_count,
        "residual_lifecycle_stage": lifecycle_stage,
        "entry_fill_timestamp": entry_fill_time,
        "residual_last_event_ts": last_event_ts,
        "realized_gross_so_far_usd": round(realized_gross_total, 8),
    }
    if carry_position_id is not None:
        base_fields["position_id"] = carry_position_id
        base_fields["position_open_timestamp"] = carry_position_open_ts or entry_fill_time

    if remaining_size > 0:
        return {
            "actual_outcome": "open",
            "review_note": "Partial exit observed; residual position still open in fetched window",
            "position_status": "open",
            "position_close_timestamp": None,
            **base_fields,
        }

    if realized_exit_size <= 0:
        return {
            "actual_outcome": "open",
            "review_note": "No stop/target hit after entry fill in fetched window",
            **base_fields,
        }

    gross_total = realized_gross_total
    gross_per_btc = gross_total / realized_exit_size
    effective_exit_price = _effective_exit_price_from_gross(direction, entry_price, gross_per_btc)

    notional = abs(entry_price * executed_size)
    fees = notional * taker_fee_rate * 2
    slippage = notional * slippage_rate * 2
    net_total = gross_total - fees - slippage

    if net_total > 0:
        outcome = "win"
    elif net_total < 0:
        outcome = "loss"
    else:
        outcome = "breakeven"

    last_exit_time = last_event_ts or entry_timestamp
    held_hrs = max(0.0, (last_exit_time - entry_fill_time).total_seconds() / 3600)
    pnl_pct = (net_total / notional) * 100 if notional > 0 else 0.0

    return {
        "actual_outcome": outcome,
        "actual_exit_price": round(effective_exit_price, 8),
        "actual_pnl_usd": net_total,
        "actual_pnl_basis": "net",
        "actual_pnl_pct": round(pnl_pct, 4),
        "held_duration_hours": round(held_hrs, 3),
        "review_note": "Suggested by v1.3 outcome_suggester",
        "executed_notional_usd": notional,
        "friction_fees_usd": round(fees, 4),
        "friction_slippage_usd": round(slippage, 4),
        "position_status": "closed",
        "position_close_timestamp": last_exit_time,
        **base_fields,
    }


def suggest_outcome_update(
    timestamp_iso: str,
    journal_path: str = "journal/journal.jsonl",
    output_path: str = "journal/outcome_patch.json",
    config_path: str | None = None,
) -> dict:
    cfg = load_strategy_config(config_path)
    entry = _load_entry_by_timestamp(journal_path, timestamp_iso)

    entry_ts = datetime.fromisoformat(str(entry.timestamp)).astimezone(timezone.utc)
    residual_cursor = None
    if str(entry.actual_outcome).lower() == "open" and (entry.residual_position_size_btc or 0.0) > 0:
        if entry.residual_last_event_ts is not None:
            residual_cursor = datetime.fromisoformat(str(entry.residual_last_event_ts)).astimezone(timezone.utc)
        else:
            residual_cursor = entry_ts

    scan_start = residual_cursor or entry_ts
    future = fetch_historical_candles(cfg, cfg.confirm_timeframe, scan_start, limit=300)
    horizon_end = scan_start + timedelta(hours=cfg.outcome_horizon_hours)
    future = [c for c in future if scan_start < c.timestamp <= horizon_end]

    low, high = literal_eval(entry.entry)
    fallback_entry = (float(low) + float(high)) / 2
    entry_price = entry.executed_entry_price if entry.executed_entry_price is not None else fallback_entry
    size = entry.position_size_btc if entry.position_size_btc > 0 else 1.0

    position_id = _derive_position_id(timestamp_iso, str(entry.decision), str(entry.entry), getattr(entry, "position_id", None))

    patch = journal_entry_payload(
        {
            "timestamp": timestamp_iso,
            "position_id": position_id,
            **derive_outcome_fields(
                direction=entry.decision,
                entry_price=entry_price,
                position_size_btc=size,
                stop=float(entry.stop),
                target=float(entry.target),
                entry_timestamp=entry_ts,
                future_candles=future,
                taker_fee_rate=cfg.taker_fee_rate,
                slippage_rate=cfg.slippage_rate,
                entry_zone=(float(low), float(high)),
                partial_take_profit_fraction=cfg.partial_take_profit_fraction,
                residual_state={
                    "carry_active": residual_cursor is not None,
                    "residual_position_size_btc": entry.residual_position_size_btc,
                    "executed_size_btc": entry.executed_size_btc,
                    "unfilled_size_btc": entry.unfilled_size_btc,
                    "target_exit_size_btc": entry.target_exit_size_btc,
                    "stop_exit_size_btc": entry.stop_exit_size_btc,
                    "realized_exit_size_btc": entry.realized_exit_size_btc,
                    "exit_fill_count": entry.exit_fill_count,
                    "residual_lifecycle_stage": entry.residual_lifecycle_stage,
                    "entry_fill_timestamp": entry.entry_fill_timestamp,
                    "realized_gross_so_far_usd": entry.realized_gross_so_far_usd,
                    "position_id": position_id,
                    "position_open_timestamp": entry.position_open_timestamp,
                },
            ),
        }
    )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(patch, indent=2, default=str), encoding="utf-8")
    return patch


def apply_outcome_patch_to_journal(
    patch: dict,
    journal_path: str = "journal/journal.jsonl",
    config: StrategyConfig | None = None,
) -> JournalEntry:
    patch_payload = journal_entry_payload(patch)
    timestamp = str(patch_payload.get("timestamp", "")).strip()
    if not timestamp:
        raise RuntimeError("Patch must include timestamp")

    outcome = str(patch_payload.get("actual_outcome", "")).lower()
    if outcome in REALIZED_OUTCOMES:
        missing = [k for k in REALIZED_PATCH_REQUIRED_FIELDS if k not in patch_payload]
        if missing:
            raise RuntimeError(f"Patch missing required realized fields: {missing}")

    p = Path(journal_path)
    if not p.exists():
        raise RuntimeError(f"Journal not found: {journal_path}")

    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    matched_indexes = [i for i, ln in enumerate(lines) if str(json.loads(ln).get("timestamp")) == timestamp]
    if len(matched_indexes) != 1:
        raise RuntimeError(f"Expected exactly 1 journal row for timestamp {timestamp}, found {len(matched_indexes)}")

    idx = matched_indexes[0]
    original = json.loads(lines[idx])
    if original.get("position_id") and patch_payload.get("position_id") and str(original.get("position_id")) != str(patch_payload.get("position_id")):
        raise RuntimeError("Position identity mismatch: patch cannot change position_id")

    merged = {**original, **patch_payload}
    merged = journal_entry_payload(merged)
    merged_entry = JournalEntry(**merged)

    check_cfg = config or StrategyConfig()
    merged_outcome = str(merged_entry.actual_outcome).lower()
    if merged_outcome in REALIZED_OUTCOMES:
        validation = validate_realized_row(merged_entry, check_cfg)
        if validation.status not in {"accepted_net", "accepted_gross_normalized"}:
            raise RuntimeError(f"Patch merge rejected by realized validation: {validation.reason}")
    else:
        if any(getattr(merged_entry, f) is not None for f in NON_REALIZED_FORBIDDEN_FIELDS):
            raise RuntimeError("Patch merge rejected: non-realized outcome cannot retain realized pnl/exit fields")

    lines[idx] = json.dumps(merged, default=str)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return merged_entry


def main() -> None:
    parser = argparse.ArgumentParser(description="Suggest trade outcome fields for a journal entry")
    parser.add_argument("--timestamp", required=True, help="Journal entry timestamp ISO string")
    parser.add_argument("--journal", default="journal/journal.jsonl")
    parser.add_argument("--output", default="journal/outcome_patch.json")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    patch = suggest_outcome_update(
        timestamp_iso=args.timestamp,
        journal_path=args.journal,
        output_path=args.output,
        config_path=args.config,
    )
    print(json.dumps(patch, indent=2))


if __name__ == "__main__":
    main()
