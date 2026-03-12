from __future__ import annotations

from .config import StrategyConfig
from .models import FinalDecision, TradeCandidate


def build_trade_candidate(
    current_price: float,
    atr: float,
    config: StrategyConfig,
    direction: FinalDecision,
) -> TradeCandidate:
    band = atr * 0.15
    if direction == FinalDecision.PAPER_LONG:
        entry = (current_price - band, current_price)
        stop = current_price - (atr * config.atr_stop_multiple)
        target = current_price + (atr * config.atr_target_multiple)
        risk = current_price - stop
        reward = target - current_price
    else:
        entry = (current_price, current_price + band)
        stop = current_price + (atr * config.atr_stop_multiple)
        target = current_price - (atr * config.atr_target_multiple)
        risk = stop - current_price
        reward = current_price - target

    rr = reward / risk if risk > 0 else 0.0
    valid = rr >= config.min_risk_reward
    reason = None if valid else "Risk/reward below threshold"

    return TradeCandidate(
        direction=direction,
        entry_zone=entry,
        stop_loss=stop,
        target=target,
        risk_reward=rr,
        valid=valid,
        invalid_reason=reason,
    )


def validate_entry_zone(
    candidate: TradeCandidate,
    current_price: float,
    atr: float,
    config: StrategyConfig,
) -> tuple[bool, str | None]:
    low, high = sorted(candidate.entry_zone)
    tolerance = atr * config.chase_atr_tolerance

    if current_price < low - tolerance or current_price > high + tolerance:
        return False, "Price moved too far beyond entry zone (no-chase rule)"
    return True, None
