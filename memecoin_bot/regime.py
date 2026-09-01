"""Market regime, measured from the breadth of what the scanner just saw.

A strategy does not work equally well everywhere, so results are filed by
the conditions they were earned in. The regime is computed from the same
scan the bot already performs -- no extra data source, nothing to fabricate.
"""

from __future__ import annotations

import statistics
from enum import Enum


class Regime(str, Enum):
    STRONG_BULL = "strong_bull"
    WEAK_BULL = "weak_bull"
    SIDEWAYS = "sideways"
    HIGH_VOLATILITY = "high_volatility"
    RISK_OFF = "risk_off"
    LOW_LIQUIDITY = "low_liquidity"
    UNKNOWN = "unknown"


MIN_BREADTH = 8
"""Below this many tokens the breadth read is noise, and the honest answer
is UNKNOWN rather than a regime derived from four coins."""


def detect(snapshots: "tuple | list") -> tuple[Regime, dict[str, float]]:
    """Classify the current environment and return the evidence for it.

    Order matters: the conditions that make trading dangerous are checked
    before the ones that make it look attractive, so a violent tape is
    never filed as a bull market just because most things are green.
    """

    usable = [s for s in snapshots if getattr(s, "price_usd", 0) > 0]
    if len(usable) < MIN_BREADTH:
        return Regime.UNKNOWN, {"tokens": float(len(usable))}

    ups_1h = [s for s in usable if s.price_change_1h > 0]
    breadth = len(ups_1h) / len(usable)
    moves = [abs(s.price_change_5m) for s in usable]
    volatility = statistics.median(moves)
    liquidity = statistics.median([s.liquidity_usd for s in usable])
    median_1h = statistics.median([s.price_change_1h for s in usable])

    evidence = {
        "tokens": float(len(usable)),
        "breadth_1h": round(breadth, 3),
        "median_abs_5m": round(volatility, 4),
        "median_1h": round(median_1h, 4),
        "median_liquidity_usd": round(liquidity, 2),
    }

    # Danger first.
    if liquidity < 15_000:
        return Regime.LOW_LIQUIDITY, evidence
    if volatility > 0.18:
        return Regime.HIGH_VOLATILITY, evidence
    if breadth < 0.25 and median_1h < -0.05:
        return Regime.RISK_OFF, evidence

    if breadth > 0.70 and median_1h > 0.05:
        return Regime.STRONG_BULL, evidence
    if breadth > 0.55:
        return Regime.WEAK_BULL, evidence
    return Regime.SIDEWAYS, evidence
