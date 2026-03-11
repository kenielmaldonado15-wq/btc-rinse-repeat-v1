from __future__ import annotations

from .config import StrategyConfig
from .models import MarketRegime


def evaluate_hard_rules(
    config: StrategyConfig,
    regime: MarketRegime,
    structure_quality: float,
    volume_quality_score: float,
    atr: float,
    timeframe_alignment_score: float,
    candidate_rr: float,
    entry_zone_valid: bool,
    entry_zone_reason: str | None,
    event_risk_active: bool,
) -> list[str]:
    reasons: list[str] = []

    if event_risk_active:
        reasons.append("Event-risk filter active")
    if regime == MarketRegime.CHOPPY:
        reasons.append("Regime is choppy/indeterminate")
    if structure_quality < config.min_structure_quality:
        reasons.append("Structure is unclear")
    if volume_quality_score < config.min_volume_quality:
        reasons.append("Volume is too weak")
    if atr < config.min_atr:
        reasons.append("ATR indicates dead market")
    if atr > config.max_atr:
        reasons.append("ATR indicates unstable market")
    if timeframe_alignment_score < config.min_alignment_score:
        reasons.append("4H and 1H conflict too heavily")
    if candidate_rr < config.min_risk_reward:
        reasons.append("Risk/reward below threshold")
    if not entry_zone_valid and entry_zone_reason:
        reasons.append(entry_zone_reason)

    return reasons
