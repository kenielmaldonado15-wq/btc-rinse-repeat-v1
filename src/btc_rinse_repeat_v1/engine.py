from __future__ import annotations

from datetime import datetime, timezone

from .config import StrategyConfig
from .direction import build_trade_candidate, validate_entry_zone
from .hard_rules import evaluate_hard_rules
from .indicators import average_true_range, simple_momentum, volume_quality
from .models import Candle, DecisionResult, FinalDecision, MarketRegime, StructureAssessment
from .regime import detect_market_regime
from .risk import conservative_entry, kill_switch_active, position_size_btc
from .scoring import confidence_score
from .structure import assess_structure


def _timeframe_alignment_score(regime: MarketRegime, momentum_1h: float) -> float:
    if regime == MarketRegime.TREND_UP:
        return 1.0 if momentum_1h > 0 else -1.0
    if regime == MarketRegime.TREND_DOWN:
        return 1.0 if momentum_1h < 0 else -1.0
    return 0.0


def _preferred_direction(regime: MarketRegime) -> FinalDecision:
    if regime == MarketRegime.TREND_UP:
        return FinalDecision.PAPER_LONG
    if regime == MarketRegime.TREND_DOWN:
        return FinalDecision.PAPER_SHORT
    return FinalDecision.STAND_ASIDE


def _event_risk_active(now: datetime, config: StrategyConfig) -> bool:
    if config.manual_event_risk_active:
        return True
    if config.event_risk_start_hour_utc is None or config.event_risk_end_hour_utc is None:
        return False

    hour = now.astimezone(timezone.utc).hour
    start = config.event_risk_start_hour_utc
    end = config.event_risk_end_hour_utc
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def run_cycle(
    candles_4h: list[Candle],
    candles_1h: list[Candle],
    config: StrategyConfig,
    account_equity_usd: float,
    now: datetime,
    kill_switch_state,
) -> DecisionResult:
    atr = average_true_range(candles_4h)
    momentum_1h = simple_momentum(candles_1h)
    volume_score = volume_quality(candles_4h)

    regime, _diag = detect_market_regime(candles_4h, atr, config)
    structure: StructureAssessment = assess_structure(candles_4h, candles_1h)
    alignment_score = _timeframe_alignment_score(regime, momentum_1h)
    event_risk_active = _event_risk_active(now, config)

    if kill_switch_active(kill_switch_state, now):
        return DecisionResult(
            timestamp=now,
            market_regime=regime,
            trade_bias="None",
            entry_zone=None,
            stop_loss=None,
            target=None,
            atr=atr,
            risk_reward_ratio=None,
            confidence_score=0.0,
            final_decision=FinalDecision.STAND_ASIDE,
            reasoning=["Kill switch active after consecutive losses"],
            position_size_btc=0.0,
            hard_rule_failures=["Kill switch active after consecutive losses"],
        )

    direction = _preferred_direction(regime)
    if direction == FinalDecision.STAND_ASIDE:
        return DecisionResult(
            timestamp=now,
            market_regime=regime,
            trade_bias="None",
            entry_zone=None,
            stop_loss=None,
            target=None,
            atr=atr,
            risk_reward_ratio=None,
            confidence_score=0.0,
            final_decision=FinalDecision.STAND_ASIDE,
            reasoning=["No directional bias in non-trending regime"],
            position_size_btc=0.0,
            hard_rule_failures=["No directional bias in non-trending regime"],
        )

    current_price = candles_4h[-1].close
    candidate = build_trade_candidate(current_price, atr, config, direction)
    zone_ok, zone_reason = validate_entry_zone(candidate, current_price, atr, config)

    hard_rule_reasons = evaluate_hard_rules(
        config=config,
        regime=regime,
        structure_quality=structure.quality,
        volume_quality_score=volume_score,
        atr=atr,
        timeframe_alignment_score=alignment_score,
        candidate_rr=candidate.risk_reward,
        entry_zone_valid=zone_ok,
        entry_zone_reason=zone_reason,
        event_risk_active=event_risk_active,
    )

    if hard_rule_reasons:
        return DecisionResult(
            timestamp=now,
            market_regime=regime,
            trade_bias=direction.value,
            entry_zone=candidate.entry_zone,
            stop_loss=candidate.stop_loss,
            target=candidate.target,
            atr=atr,
            risk_reward_ratio=candidate.risk_reward,
            confidence_score=0.0,
            final_decision=FinalDecision.STAND_ASIDE,
            reasoning=structure.notes + hard_rule_reasons,
            position_size_btc=0.0,
            hard_rule_failures=hard_rule_reasons,
        )

    regime_clarity = 1.0 if regime in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN) else 0.4
    trend_component = 1.0 if direction == FinalDecision.PAPER_LONG else -1.0
    score = confidence_score(
        trend=trend_component,
        structure=structure.quality,
        momentum=momentum_1h,
        volume=volume_score,
        regime_clarity=regime_clarity,
        risk_reward=candidate.risk_reward,
        min_rr=config.min_risk_reward,
    )

    if score < config.scoring_threshold:
        decision = FinalDecision.STAND_ASIDE
        position_size = 0.0
        reasoning = structure.notes + ["Scoring layer confidence below threshold"]
    else:
        decision = candidate.direction
        entry_for_sizing = conservative_entry(candidate.entry_zone, decision)
        position_size = position_size_btc(
            account_equity_usd,
            config.per_trade_risk_fraction,
            entry_for_sizing,
            candidate.stop_loss,
        )
        reasoning = structure.notes + ["Hard rules passed", "Score passed threshold"]

    return DecisionResult(
        timestamp=now,
        market_regime=regime,
        trade_bias=direction.value,
        entry_zone=candidate.entry_zone,
        stop_loss=candidate.stop_loss,
        target=candidate.target,
        atr=atr,
        risk_reward_ratio=candidate.risk_reward,
        confidence_score=score,
        final_decision=decision,
        reasoning=reasoning,
        position_size_btc=position_size,
        hard_rule_failures=[],
    )


def should_exit_early(
    current_price: float,
    entry_price: float,
    atr: float,
    direction: FinalDecision,
    structure: StructureAssessment,
    config: StrategyConfig,
) -> bool:
    move_against = (entry_price - current_price) if direction == FinalDecision.PAPER_LONG else (current_price - entry_price)
    atr_breach = move_against > (atr * config.early_exit_atr_multiple)

    if not config.early_exit_require_structure_break:
        return atr_breach

    structure_break = structure.retest_fail or structure.rejection_failed_reclaim
    return atr_breach and structure_break
