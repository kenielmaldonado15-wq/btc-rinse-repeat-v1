# BTC Rinse & Repeat v1.3 (Validation)

Paper-trading BTC decision system using a **Rules-First, Scoring-Second** architecture.

## Quick start

```bash
pip install -e .
PYTHONPATH=src python -m btc_rinse_repeat_v1.main --config local.config.json
```

## v1.3 validation additions

- Outcome suggestion script (`outcome_suggester.py`) to propose post-trade journal fields by checking stop/target hit order from subsequent BTC candles.
- Simple backtest harness (`backtest.py`) that replays historical BTC data through the same engine pipeline.
- Kill-switch state persistence added to runtime state file.
- Backward-compatible journal support preserved.

## Outcome suggestion

```bash
PYTHONPATH=src python -m btc_rinse_repeat_v1.outcome_suggester \
  --timestamp "2026-03-09T00:00:00+00:00" \
  --journal journal/journal.jsonl \
  --output journal/outcome_patch.json
```

## Backtest

```bash
PYTHONPATH=src python -m btc_rinse_repeat_v1.backtest \
  --start "2025-01-01T00:00:00+00:00" \
  --output journal/backtest.jsonl
```
