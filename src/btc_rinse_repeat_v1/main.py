from __future__ import annotations

import argparse
from datetime import datetime, timezone

from .config import load_strategy_config
from .data_feed import fetch_latest_candles, should_poll
from .engine import run_cycle
from .journal import _is_sparse_supportable_legacy_entry, append_journal_jsonl, build_journal_entry, is_low_context_legacy_entry, load_validated_realized_rows, order_accepted_realized_rows
from .models import KillSwitchState
from .risk import update_kill_switch
from .runtime_state import RuntimeState, load_runtime_state, save_runtime_state


def _refresh_kill_switch(kill_switch: KillSwitchState, now: datetime) -> KillSwitchState:
    if kill_switch.stand_aside_until and now >= kill_switch.stand_aside_until:
        kill_switch.consecutive_losses = 0
        kill_switch.review_required = False
        kill_switch.stand_aside_until = None
    return kill_switch


def _kill_switch_has_persisted_progress(kill_switch: KillSwitchState | None) -> bool:
    if kill_switch is None:
        return False
    return (
        kill_switch.consecutive_losses > 0
        or kill_switch.review_required
        or kill_switch.stand_aside_until is not None
    )




def _legacy_replay_event_key(entry) -> str:
    ts = datetime.fromisoformat(str(entry.timestamp))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return "|".join(
        [
            ts.isoformat(),
            str(entry.decision),
            str(entry.entry),
            str(entry.stop),
            str(entry.target),
            str(entry.executed_entry_price),
            str(entry.actual_exit_price),
            str(entry.actual_outcome),
            str(entry.actual_pnl_basis),
        ]
    )
def _apply_realized_outcomes_to_kill_switch(
    kill_switch: KillSwitchState,
    last_processed_outcome_ts: datetime | None,
    accepted_realized,
    config,
    processed_closed_position_ids: set[str] | None = None,
    processed_legacy_event_keys: set[str] | None = None,
) -> tuple[KillSwitchState, datetime | None]:
    processed_closed_position_ids = processed_closed_position_ids if processed_closed_position_ids is not None else set()
    processed_legacy_event_keys = processed_legacy_event_keys if processed_legacy_event_keys is not None else set()
    normalized_cursor = last_processed_outcome_ts
    if normalized_cursor is not None:
        if normalized_cursor.tzinfo is None:
            normalized_cursor = normalized_cursor.replace(tzinfo=timezone.utc)
        else:
            normalized_cursor = normalized_cursor.astimezone(timezone.utc)

    last_ts = normalized_cursor
    ordered = order_accepted_realized_rows([r for r in accepted_realized if r.normalized_net_pnl_usd is not None])
    for row in ordered:
        ts = datetime.fromisoformat(str(row.entry.timestamp))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        if normalized_cursor and ts <= normalized_cursor:
            continue

        position_id = str(getattr(row.entry, "position_id", "") or "").strip()
        position_status = str(getattr(row.entry, "position_status", "") or "").lower().strip()
        if position_id:
            if position_id in processed_closed_position_ids:
                continue
            if position_status and position_status != "closed":
                # fail closed: a position-scoped realized event should only impact runtime kill-switch once fully closed
                continue
        else:
            policy = str(getattr(config, "runtime_legacy_row_policy", "upgrade_deterministic") or "upgrade_deterministic").lower().strip()
            if policy == "reject":
                continue
            low_context_policy = str(getattr(config, "runtime_low_context_legacy_policy", "bound_event_only") or "bound_event_only").lower().strip()
            if is_low_context_legacy_entry(row.entry) and low_context_policy == "reject":
                continue
            sparse_policy = str(getattr(config, "runtime_sparse_history_policy", "reconstruct_supportable") or "reconstruct_supportable").lower().strip()
            if _is_sparse_supportable_legacy_entry(row.entry) and sparse_policy == "reject":
                continue
            legacy_key = _legacy_replay_event_key(row.entry)
            if legacy_key in processed_legacy_event_keys:
                continue

        update_kill_switch(kill_switch, config, float(row.normalized_net_pnl_usd), ts)
        if position_id:
            processed_closed_position_ids.add(position_id)
        else:
            processed_legacy_event_keys.add(_legacy_replay_event_key(row.entry))
        last_ts = ts
    return kill_switch, last_ts


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC Rinse & Repeat runtime")
    parser.add_argument("--config", default=None, help="Optional JSON config path")
    parser.add_argument("--state-file", default="runtime_state/state.json", help="Runtime state file path")
    parser.add_argument("--journal", default="journal/journal.jsonl", help="Journal path for realized outcomes")
    args = parser.parse_args()

    config = load_strategy_config(args.config)
    now = datetime.now(timezone.utc)
    state = load_runtime_state(args.state_file)
    state.kill_switch = _refresh_kill_switch(state.kill_switch or KillSwitchState(), now)
    state.processed_closed_position_ids = state.processed_closed_position_ids or []
    state.processed_legacy_event_keys = state.processed_legacy_event_keys or []

    accepted_realized, _rejected = load_validated_realized_rows(args.journal, config)
    if state.last_processed_outcome_ts is None and _kill_switch_has_persisted_progress(state.kill_switch) and accepted_realized:
        raise RuntimeError("incoherent_runtime_state_missing_outcome_cursor")

    if state.last_processed_outcome_ts is None and (state.processed_closed_position_ids or state.processed_legacy_event_keys):
        raise RuntimeError("incoherent_runtime_state_missing_outcome_cursor")

    processed_positions = set(state.processed_closed_position_ids)
    processed_legacy = set(state.processed_legacy_event_keys)
    state.kill_switch, state.last_processed_outcome_ts = _apply_realized_outcomes_to_kill_switch(
        state.kill_switch,
        state.last_processed_outcome_ts,
        accepted_realized,
        config,
        processed_closed_position_ids=processed_positions,
        processed_legacy_event_keys=processed_legacy,
    )
    state.processed_closed_position_ids = sorted(processed_positions)
    state.processed_legacy_event_keys = sorted(processed_legacy)

    if not should_poll(state.last_run_utc, config, now):
        save_runtime_state(state, args.state_file)
        print("Skip cycle: poll interval not reached.")
        return

    try:
        candles_4h, candles_1h, run_time = fetch_latest_candles(config)
    except Exception as exc:
        save_runtime_state(state, args.state_file)
        print(f"Data fetch failed after retries: {exc}")
        return

    result = run_cycle(
        candles_4h=candles_4h,
        candles_1h=candles_1h,
        config=config,
        account_equity_usd=config.starting_equity_usd,
        now=run_time,
        kill_switch_state=state.kill_switch,
    )
    journal = build_journal_entry(result)
    journal_path = append_journal_jsonl(journal, args.journal)
    state.last_run_utc = run_time
    save_runtime_state(state, args.state_file)

    print("Decision:", result.final_decision.value)
    print("Confidence:", round(result.confidence_score, 3))
    print("Journal path:", journal_path)


if __name__ == "__main__":
    main()
