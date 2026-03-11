from __future__ import annotations

from .models import Candle, StructureAssessment


def _swing_points(candles: list[Candle], window: int = 2) -> tuple[list[float], list[float]]:
    highs: list[float] = []
    lows: list[float] = []
    if len(candles) < window * 2 + 1:
        return highs, lows

    for i in range(window, len(candles) - window):
        center = candles[i]
        left = candles[i - window : i]
        right = candles[i + 1 : i + window + 1]

        if all(center.high > c.high for c in left + right):
            highs.append(center.high)
        if all(center.low < c.low for c in left + right):
            lows.append(center.low)

    return highs[-5:], lows[-5:]


def assess_structure(candles_4h: list[Candle], candles_1h: list[Candle]) -> StructureAssessment:
    highs, lows = _swing_points(candles_4h)
    if len(candles_4h) < 3:
        return StructureAssessment(highs, lows, False, False, False, False, False, 0.0, ["Insufficient candles"])

    close_4h = candles_4h[-1].close
    low_4h = candles_4h[-1].low
    high_4h = candles_4h[-1].high
    close_1h = candles_1h[-1].close if candles_1h else close_4h

    last_swing_high = highs[-1] if highs else None
    last_swing_low = lows[-1] if lows else None

    breakout = bool(last_swing_high is not None and close_4h > last_swing_high)
    breakdown = bool(last_swing_low is not None and close_4h < last_swing_low)

    retest_hold = False
    retest_fail = False
    rejection_failed_reclaim = False

    if breakout and last_swing_high is not None:
        retest_hold = low_4h <= last_swing_high and close_4h >= last_swing_high and close_1h >= last_swing_high
        retest_fail = close_1h < last_swing_high

    if breakdown and last_swing_low is not None:
        retest_hold = high_4h >= last_swing_low and close_4h <= last_swing_low and close_1h <= last_swing_low
        retest_fail = close_1h > last_swing_low

    if last_swing_high is not None and close_4h > last_swing_high and close_1h < last_swing_high:
        rejection_failed_reclaim = True
    if last_swing_low is not None and close_4h < last_swing_low and close_1h > last_swing_low:
        rejection_failed_reclaim = True

    notes: list[str] = []
    score = 0.45

    if breakout:
        notes.append("Breakout detected")
        score += 0.20
    if breakdown:
        notes.append("Breakdown detected")
        score += 0.20
    if retest_hold:
        notes.append("Retest hold")
        score += 0.20
    if retest_fail:
        notes.append("Retest fail")
        score -= 0.20
    if rejection_failed_reclaim:
        notes.append("Rejection / failed reclaim")
        score -= 0.20

    if not highs or not lows:
        notes.append("Weak swing map")
        score -= 0.15

    quality = max(0.0, min(1.0, score))

    return StructureAssessment(
        swing_highs=highs,
        swing_lows=lows,
        breakout=breakout,
        breakdown=breakdown,
        retest_hold=retest_hold,
        retest_fail=retest_fail,
        rejection_failed_reclaim=rejection_failed_reclaim,
        quality=quality,
        notes=notes,
    )
