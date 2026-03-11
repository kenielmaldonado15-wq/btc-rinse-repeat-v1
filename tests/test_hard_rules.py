from btc_rinse_repeat_v1.config import StrategyConfig
from btc_rinse_repeat_v1.hard_rules import evaluate_hard_rules
from btc_rinse_repeat_v1.models import MarketRegime


def test_hard_rules_block_choppy_and_low_volume() -> None:
    reasons = evaluate_hard_rules(
        config=StrategyConfig(),
        regime=MarketRegime.CHOPPY,
        structure_quality=0.8,
        volume_quality_score=0.1,
        atr=250,
        timeframe_alignment_score=1.0,
        candidate_rr=2.0,
        entry_zone_valid=True,
        entry_zone_reason=None,
        event_risk_active=False,
    )
    assert "Regime is choppy/indeterminate" in reasons
    assert "Volume is too weak" in reasons


def test_hard_rules_entry_zone_no_chase() -> None:
    reasons = evaluate_hard_rules(
        config=StrategyConfig(),
        regime=MarketRegime.TREND_UP,
        structure_quality=0.8,
        volume_quality_score=0.8,
        atr=250,
        timeframe_alignment_score=1.0,
        candidate_rr=2.0,
        entry_zone_valid=False,
        entry_zone_reason="Price moved too far beyond entry zone (no-chase rule)",
        event_risk_active=False,
    )
    assert "Price moved too far beyond entry zone (no-chase rule)" in reasons


def test_hard_rules_event_risk_forces_stand_aside_reason() -> None:
    reasons = evaluate_hard_rules(
        config=StrategyConfig(),
        regime=MarketRegime.TREND_UP,
        structure_quality=0.8,
        volume_quality_score=0.8,
        atr=250,
        timeframe_alignment_score=1.0,
        candidate_rr=2.0,
        entry_zone_valid=True,
        entry_zone_reason=None,
        event_risk_active=True,
    )
    assert "Event-risk filter active" in reasons
