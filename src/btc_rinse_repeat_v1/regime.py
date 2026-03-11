from __future__ import annotations

from dataclasses import dataclass

from .config import StrategyConfig
from .indicators import adx, directional_efficiency, trend_slope
from .models import Candle, MarketRegime


@dataclass
class RegimeDiagnostics:
    adx_value: float
    slope: float
    efficiency: float
    atr_in_bounds: bool


def detect_market_regime(candles_4h: list[Candle], atr: float, config: StrategyConfig) -> tuple[MarketRegime, RegimeDiagnostics]:
    slope = trend_slope(candles_4h)
    adx_value = adx(candles_4h)
    efficiency = directional_efficiency(candles_4h)
    atr_in_bounds = config.min_atr <= atr <= config.max_atr

    diag = RegimeDiagnostics(adx_value=adx_value, slope=slope, efficiency=efficiency, atr_in_bounds=atr_in_bounds)

    if not atr_in_bounds:
        return MarketRegime.CHOPPY, diag

    if adx_value >= config.regime_adx_trend_min and efficiency >= config.regime_chop_max_efficiency:
        if slope >= config.regime_slope_min:
            return MarketRegime.TREND_UP, diag
        if slope <= -config.regime_slope_min:
            return MarketRegime.TREND_DOWN, diag

    if adx_value < config.regime_adx_trend_min and efficiency < config.regime_chop_max_efficiency:
        return MarketRegime.CHOPPY, diag

    return MarketRegime.RANGE, diag
