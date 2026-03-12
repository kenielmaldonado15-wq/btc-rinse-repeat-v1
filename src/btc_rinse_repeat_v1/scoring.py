from __future__ import annotations


def normalized_score(value: float, lower: float = -1.0, upper: float = 1.0) -> float:
    if upper == lower:
        return 0.0
    clipped = max(lower, min(upper, value))
    return (clipped - lower) / (upper - lower)


def confidence_score(
    trend: float,
    structure: float,
    momentum: float,
    volume: float,
    regime_clarity: float,
    risk_reward: float,
    min_rr: float,
) -> float:
    rr_score = min(1.0, risk_reward / (min_rr * 2)) if min_rr > 0 else 0.0
    components = [
        normalized_score(trend),
        structure,
        normalized_score(momentum),
        volume,
        regime_clarity,
        rr_score,
    ]
    return sum(components) / len(components)
