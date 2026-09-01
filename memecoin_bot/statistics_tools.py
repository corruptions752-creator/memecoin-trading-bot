"""Small statistics helpers shared by the evidence-weighing agents.

Kept in one place so every agent reports uncertainty the same way. The
recurring failure this guards against is a flattering win rate computed
from a handful of trades and then quoted as if it were a fact.
"""

from __future__ import annotations

import math

MIN_MEANINGFUL_SAMPLE = 30
"""Below this, a win rate is quoted only with its interval and never used
to justify raising confidence. Chosen to be blunt rather than clever: the
repository has already been burned by a 30-seed result that reversed at 70."""


def wilson_interval(
    wins: int, n: int, z: float = 1.96
) -> tuple[float, float, float]:
    """Return (rate, low, high) for a binomial proportion.

    Wilson rather than the textbook normal interval because the samples
    here are small and often near 0 or 1, where the normal interval
    happily returns bounds outside [0, 1].
    """

    if n <= 0:
        return 0.0, 0.0, 1.0
    rate = wins / n
    denominator = 1 + z * z / n
    centre = rate + z * z / (2 * n)
    spread = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n))
    return rate, max(0.0, (centre - spread) / denominator), min(
        1.0, (centre + spread) / denominator
    )


def expectancy(results: "list[float]") -> float:
    """Average dollars per trade. Negative means it loses money."""

    return sum(results) / len(results) if results else 0.0


def profit_factor(results: "list[float]") -> float:
    """Gross win over gross loss. inf when nothing lost, 0 when nothing won."""

    won = sum(r for r in results if r > 0)
    lost = -sum(r for r in results if r < 0)
    if lost == 0:
        return float("inf") if won > 0 else 0.0
    return won / lost


def mean_without_best(results: "list[float]") -> float:
    """Mean with the single best observation dropped.

    The check that has repeatedly separated a real effect from one lucky
    outlier in this repository. A result that collapses when its best
    sample is removed was that sample.
    """

    if len(results) < 2:
        return float("nan")
    return sum(sorted(results)[:-1]) / (len(results) - 1)


def split_train_validation(
    records: "list", validation_fraction: float = 0.3
) -> "tuple[list, list]":
    """Split chronologically, oldest first.

    Chronological rather than random: a random split leaks the future into
    the training half, and every trade here is a time series point.
    """

    if len(records) < 4:
        return list(records), []
    cut = int(len(records) * (1 - validation_fraction))
    return list(records[:cut]), list(records[cut:])
