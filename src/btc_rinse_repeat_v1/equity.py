from __future__ import annotations

from .config import StrategyConfig
from .models import JournalEntry, SimulatedEquityState


def apply_trading_friction(gross_pnl_usd: float, notional_usd: float, config: StrategyConfig) -> float:
    fees = notional_usd * config.taker_fee_rate * 2
    slippage = notional_usd * config.slippage_rate * 2
    return gross_pnl_usd - fees - slippage


def _update_drawdown(state: SimulatedEquityState) -> None:
    if state.current_equity_usd > state.high_water_mark_usd:
        state.high_water_mark_usd = state.current_equity_usd

    drawdown_usd = max(0.0, state.high_water_mark_usd - state.current_equity_usd)
    if drawdown_usd > state.max_drawdown_usd:
        state.max_drawdown_usd = drawdown_usd

    if state.high_water_mark_usd > 0:
        drawdown_pct = (drawdown_usd / state.high_water_mark_usd) * 100
        if drawdown_pct > state.max_drawdown_pct:
            state.max_drawdown_pct = drawdown_pct


def _coherent_realized_pnl(entry: JournalEntry, config: StrategyConfig) -> float | None:
    if entry.actual_pnl_usd is None:
        return None

    # v1.3 truth model: actual_pnl_usd is net. Backward fallback supports legacy gross rows.
    if str(entry.actual_pnl_basis).lower() == "gross":
        notional = entry.executed_notional_usd or 0.0
        return apply_trading_friction(entry.actual_pnl_usd, notional, config)
    return entry.actual_pnl_usd


def update_equity_from_entry(
    state: SimulatedEquityState,
    entry: JournalEntry,
    config: StrategyConfig,
) -> SimulatedEquityState:
    realized = _coherent_realized_pnl(entry, config)
    if realized is None:
        return state

    state.current_equity_usd += realized

    if realized < 0:
        state.current_consecutive_losses += 1
        state.max_consecutive_losses = max(state.max_consecutive_losses, state.current_consecutive_losses)
    elif realized > 0:
        state.current_consecutive_losses = 0

    _update_drawdown(state)
    return state
