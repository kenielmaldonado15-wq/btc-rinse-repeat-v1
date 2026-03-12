from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import load_strategy_config
from .data_feed import fetch_historical_candles
from .engine import run_cycle
from .journal import build_journal_entry, journal_entry_payload
from .models import FinalDecision, KillSwitchState
from .outcome_suggester import derive_outcome_fields
from .risk import conservative_entry



def run_backtest(start_iso: str, output_path: str = "journal/backtest.jsonl", config_path: str | None = None) -> Path:
    cfg = load_strategy_config(config_path)
    start = datetime.fromisoformat(start_iso).astimezone(timezone.utc)

    candles_4h = fetch_historical_candles(cfg, cfg.primary_timeframe, start, limit=300)
    candles_1h = fetch_historical_candles(cfg, cfg.confirm_timeframe, start, limit=700)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    kill_switch = KillSwitchState()
    with out.open("w", encoding="utf-8") as f:
        for idx in range(60, len(candles_4h)):
            now_candle = candles_4h[idx]
            history_4h = candles_4h[: idx + 1]
            history_1h = [c for c in candles_1h if c.timestamp <= now_candle.timestamp]
            if len(history_1h) < 80:
                continue

            decision = run_cycle(
                candles_4h=history_4h,
                candles_1h=history_1h,
                config=cfg,
                account_equity_usd=cfg.starting_equity_usd,
                now=now_candle.timestamp,
                kill_switch_state=kill_switch,
            )

            horizon_end = now_candle.timestamp + timedelta(hours=cfg.outcome_horizon_hours)
            horizon_1h = [c for c in candles_1h if now_candle.timestamp < c.timestamp <= horizon_end]

            if (
                decision.final_decision in (FinalDecision.PAPER_LONG, FinalDecision.PAPER_SHORT)
                and decision.entry_zone is not None
                and decision.stop_loss is not None
                and decision.target is not None
            ):
                entry_px = conservative_entry(decision.entry_zone, decision.final_decision)
                outcome = derive_outcome_fields(
                    direction=decision.final_decision.value,
                    entry_price=entry_px,
                    position_size_btc=decision.position_size_btc,
                    stop=decision.stop_loss,
                    target=decision.target,
                    entry_timestamp=decision.timestamp,
                    future_candles=horizon_1h,
                    taker_fee_rate=cfg.taker_fee_rate,
                    slippage_rate=cfg.slippage_rate,
                    entry_zone=decision.entry_zone,
                    partial_take_profit_fraction=cfg.partial_take_profit_fraction,
                )
            else:
                outcome = {"actual_outcome": "stand_aside"}

            row = build_journal_entry(decision).__dict__
            row.update(outcome)
            row = journal_entry_payload(row)
            f.write(json.dumps(row, default=str) + "\n")

    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Simple BTC backtest harness using existing engine")
    parser.add_argument("--start", required=True, help="ISO start datetime, e.g. 2025-01-01T00:00:00+00:00")
    parser.add_argument("--output", default="journal/backtest.jsonl")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    path = run_backtest(args.start, args.output, args.config)
    print(f"Backtest written: {path}")


if __name__ == "__main__":
    main()
