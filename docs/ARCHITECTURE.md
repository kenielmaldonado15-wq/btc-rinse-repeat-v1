# BTC Rinse & Repeat v1.3 Validation Architecture

## Core runtime flow (unchanged architecture)

1. Load strategy config (defaults + optional JSON overrides).
2. Load persistent runtime state (`runtime_state/state.json`) including:
   - `last_run_utc`
   - `kill_switch` state
3. Poll gate checks run cadence.
4. Fetch BTC/USDT candles via CCXT Binance (4H + 1H) with retry and rate limit safety.
5. Execute existing rules-first -> scoring-second engine.
6. Append decision journal row (JSONL).
7. Persist updated runtime state.

## v1.3 additions

### 1) Outcome suggester

- Input: journal timestamp (existing row), journal path.
- Fetches subsequent 1H BTC candles from decision timestamp.
- Evaluates which is hit first:
  - stop
  - target
- Emits merge-ready JSON patch fields:
  - `actual_outcome`
  - `actual_exit_price`
  - `actual_pnl_usd`
  - `actual_pnl_pct`
  - `held_duration_hours`

### 2) Backtest harness

- Fetch historical 4H + 1H BTC candles.
- Replay windows through the same `run_cycle` engine.
- Log hypothetical decisions and simple stop/target-first outcome suggestions to `backtest.jsonl`.

## Compatibility

- Journal format remains backward compatible.
- Core engine architecture is preserved.
