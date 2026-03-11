from datetime import datetime, timedelta

from btc_rinse_repeat_v1.config import StrategyConfig
from btc_rinse_repeat_v1.indicators import average_true_range
from btc_rinse_repeat_v1.models import Candle, MarketRegime
from btc_rinse_repeat_v1.regime import detect_market_regime
from btc_rinse_repeat_v1.structure import assess_structure


def _trend_candles(n: int = 60, start: float = 50000, step: float = 120) -> list[Candle]:
    now = datetime.utcnow()
    price = start
    out: list[Candle] = []
    for i in range(n):
        o = price
        c = price + step
        out.append(
            Candle(
                timestamp=now + timedelta(hours=i * 4),
                open=o,
                high=max(o, c) + 50,
                low=min(o, c) - 50,
                close=c,
                volume=200 + i,
            )
        )
        price = c
    return out


def test_regime_trend_up_with_adx_and_efficiency() -> None:
    candles = _trend_candles()
    atr = average_true_range(candles)
    regime, _ = detect_market_regime(candles, atr, StrategyConfig())
    assert regime == MarketRegime.TREND_UP


def test_structure_has_swing_map_and_quality_bounds() -> None:
    candles_4h = _trend_candles(80)
    candles_1h = _trend_candles(100, start=candles_4h[-1].close, step=30)
    s = assess_structure(candles_4h, candles_1h)
    assert 0.0 <= s.quality <= 1.0
    assert isinstance(s.swing_highs, list)
    assert isinstance(s.swing_lows, list)
