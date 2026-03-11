from datetime import datetime, timedelta

from btc_rinse_repeat_v1.config import StrategyConfig
from btc_rinse_repeat_v1.models import KillSwitchState
from btc_rinse_repeat_v1.risk import kill_switch_active, update_kill_switch


def test_kill_switch_activates_after_three_losses() -> None:
    config = StrategyConfig()
    now = datetime.utcnow()
    state = KillSwitchState()

    state = update_kill_switch(state, config, trade_pnl=-1, now=now)
    state = update_kill_switch(state, config, trade_pnl=-1, now=now + timedelta(minutes=5))
    state = update_kill_switch(state, config, trade_pnl=-1, now=now + timedelta(minutes=10))

    assert state.review_required is True
    assert state.stand_aside_until is not None
    assert kill_switch_active(state, now + timedelta(hours=1)) is True
