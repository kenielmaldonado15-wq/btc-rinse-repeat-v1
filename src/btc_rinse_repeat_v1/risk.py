from __future__ import annotations

from datetime import datetime, timedelta

from .config import StrategyConfig
from .models import FinalDecision, KillSwitchState


def conservative_entry(entry_zone: tuple[float, float], direction: FinalDecision) -> float:
    low, high = sorted(entry_zone)
    if direction == FinalDecision.PAPER_LONG:
        return high
    if direction == FinalDecision.PAPER_SHORT:
        return low
    return (low + high) / 2


def position_size_btc(account_equity_usd: float, risk_fraction: float, entry: float, stop: float) -> float:
    risk_capital = account_equity_usd * risk_fraction
    risk_per_btc = abs(entry - stop)
    if risk_per_btc <= 0:
        return 0.0
    return risk_capital / risk_per_btc


def update_kill_switch(
    state: KillSwitchState,
    config: StrategyConfig,
    trade_pnl: float,
    now: datetime,
) -> KillSwitchState:
    if state.stand_aside_until and now < state.stand_aside_until:
        return state

    if trade_pnl < 0:
        state.consecutive_losses += 1
    elif trade_pnl > 0:
        state.consecutive_losses = 0

    if state.consecutive_losses >= config.kill_switch_consecutive_losses:
        state.stand_aside_until = now + timedelta(hours=config.kill_switch_hours)
        state.review_required = True

    return state


def kill_switch_active(state: KillSwitchState, now: datetime) -> bool:
    if state.stand_aside_until is None:
        return False
    return now < state.stand_aside_until or state.review_required
