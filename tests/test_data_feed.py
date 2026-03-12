from datetime import datetime, timezone

from btc_rinse_repeat_v1.config import StrategyConfig
from btc_rinse_repeat_v1.data_feed import _to_candles, should_poll


def test_to_candles_converts_ohlcv_rows() -> None:
    rows = [[1700000000000, 100, 110, 95, 105, 1234]]
    candles = _to_candles(rows)
    assert len(candles) == 1
    assert candles[0].close == 105.0


def test_should_poll_interval() -> None:
    cfg = StrategyConfig(poll_interval_minutes=10)
    now = datetime.now(timezone.utc)
    assert should_poll(None, cfg, now) is True
    assert should_poll(now, cfg, now) is False
