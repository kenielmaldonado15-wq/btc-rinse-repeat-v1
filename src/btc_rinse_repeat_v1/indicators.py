from __future__ import annotations

from statistics import mean

from .models import Candle


def average_true_range(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0

    trs: list[float] = []
    for i in range(1, len(candles)):
        current = candles[i]
        prev = candles[i - 1]
        tr = max(
            current.high - current.low,
            abs(current.high - prev.close),
            abs(current.low - prev.close),
        )
        trs.append(tr)

    return mean(trs[-period:]) if trs else 0.0


def simple_momentum(candles: list[Candle], lookback: int = 10) -> float:
    if len(candles) <= lookback:
        return 0.0
    first = candles[-lookback - 1].close
    last = candles[-1].close
    if first == 0:
        return 0.0
    return (last - first) / first


def volume_quality(candles: list[Candle], lookback: int = 20) -> float:
    if len(candles) < lookback * 2:
        return 0.0

    recent = [c.volume for c in candles[-lookback:]]
    prior = [c.volume for c in candles[-lookback * 2 : -lookback]]
    prior_avg = mean(prior) if prior else 0.0
    if prior_avg == 0:
        return 0.0

    ratio = mean(recent) / prior_avg
    return max(0.0, min(1.0, ratio / 1.5))


def trend_slope(candles: list[Candle], lookback: int = 20) -> float:
    if len(candles) <= lookback:
        return 0.0
    start = candles[-lookback - 1].close
    end = candles[-1].close
    if start == 0:
        return 0.0
    return (end - start) / start


def directional_efficiency(candles: list[Candle], lookback: int = 20) -> float:
    if len(candles) <= lookback:
        return 0.0
    segment = candles[-lookback - 1 :]
    net = abs(segment[-1].close - segment[0].close)
    travel = sum(abs(segment[i].close - segment[i - 1].close) for i in range(1, len(segment)))
    if travel == 0:
        return 0.0
    return net / travel


def adx(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < (period * 2 + 1):
        return 0.0

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr_values: list[float] = []

    for i in range(1, len(candles)):
        curr = candles[i]
        prev = candles[i - 1]

        up_move = curr.high - prev.high
        down_move = prev.low - curr.low

        plus = up_move if up_move > down_move and up_move > 0 else 0.0
        minus = down_move if down_move > up_move and down_move > 0 else 0.0

        tr = max(
            curr.high - curr.low,
            abs(curr.high - prev.close),
            abs(curr.low - prev.close),
        )

        plus_dm.append(plus)
        minus_dm.append(minus)
        tr_values.append(tr)

    dx_values: list[float] = []
    for i in range(period - 1, len(tr_values)):
        tr_sum = sum(tr_values[i - period + 1 : i + 1])
        if tr_sum == 0:
            dx_values.append(0.0)
            continue

        plus_di = 100 * (sum(plus_dm[i - period + 1 : i + 1]) / tr_sum)
        minus_di = 100 * (sum(minus_dm[i - period + 1 : i + 1]) / tr_sum)
        denom = plus_di + minus_di
        dx = 0.0 if denom == 0 else (abs(plus_di - minus_di) / denom) * 100
        dx_values.append(dx)

    if len(dx_values) < period:
        return 0.0

    return mean(dx_values[-period:])
