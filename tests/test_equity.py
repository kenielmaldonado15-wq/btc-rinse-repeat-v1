from datetime import datetime

from btc_rinse_repeat_v1.config import StrategyConfig
from btc_rinse_repeat_v1.equity import update_equity_from_entry
from btc_rinse_repeat_v1.models import JournalEntry, SimulatedEquityState


def _entry(pnl: float | None, basis: str = "net") -> JournalEntry:
    return JournalEntry(
        timestamp=datetime.utcnow(),
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100.0,110.0)",
        stop="90.0",
        target="130.0",
        projected_rr=2.0,
        confidence=0.8,
        reasoning="ok",
        what_would_have_made_this_stand_aside="none",
        position_size_btc=1.0,
        executed_entry_price=110.0,
        executed_notional_usd=110.0,
        actual_pnl_usd=pnl,
        actual_pnl_basis=basis,
        actual_outcome="win" if pnl and pnl > 0 else "loss" if pnl and pnl < 0 else "TBD",
    )


def test_equity_updates_drawdown_and_consecutive_losses() -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    state = SimulatedEquityState(1000, 1000, 1000)
    update_equity_from_entry(state, _entry(-100), cfg)
    update_equity_from_entry(state, _entry(-50), cfg)
    update_equity_from_entry(state, _entry(200), cfg)
    assert state.current_equity_usd == 1050
    assert state.max_consecutive_losses == 2
    assert state.high_water_mark_usd == 1050
    assert state.max_drawdown_usd == 150


def test_equity_applies_friction_only_for_legacy_gross_basis() -> None:
    cfg = StrategyConfig(taker_fee_rate=0.001, slippage_rate=0.001)
    state = SimulatedEquityState(1000, 1000, 1000)
    update_equity_from_entry(state, _entry(10, basis="gross"), cfg)
    # gross 10 - friction(110*0.002*2)=10-0.44 = 9.56
    assert round(state.current_equity_usd, 2) == 1009.56
