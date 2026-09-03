"""Size a playbook by what its own live record says it is worth.

The mandate is capital preservation, and the record says no playbook here
has an edge: 45 closed trades at -$9.82 expectancy, all three negative. The
honest response to that is not to keep sizing every thesis the same.

The tension this resolves: a strategy needs trades to prove itself, but a
losing strategy should not be funded while it does. So a playbook with a
demonstrated negative record is throttled rather than switched off -- it
keeps a small allocation so evidence keeps accumulating, and stops costing
real size while it accumulates. A playbook that proves itself gets its full
allocation back automatically, by the same rule.
"""

from __future__ import annotations

MIN_JUDGEABLE = 20
"""Trades before a record is allowed to change anyone's size. Below this a
losing run is indistinguishable from variance, and throttling on it would
be exactly the "change the rules after every loss" failure."""

EXPLORATION_FLOOR = 0.25
"""A throttled playbook keeps this share of normal size. Not zero: cutting
a thesis to nothing freezes its evidence at the moment it looked worst, and
it can never earn its way back."""


def size_multiplier(records: "list", playbook: str) -> tuple[float, str]:
    """Return (multiplier, one-line reason) for this playbook's next trade.

    ``records`` are closed trade_features rows.
    """

    mine = [r for r in records if r["strategy"] == playbook]
    n = len(mine)
    if n < MIN_JUDGEABLE:
        return 1.0, (
            f"{playbook}: {n} closed trades, under the {MIN_JUDGEABLE} needed "
            "to judge it — full size, no verdict"
        )

    results = [r["realized_usd"] or 0.0 for r in mine]
    per_trade = sum(results) / n
    wins = sum(1 for r in mine if r["win"])

    if per_trade <= 0:
        return EXPLORATION_FLOOR, (
            f"{playbook}: ${per_trade:+,.2f}/trade over {n} trades "
            f"({wins}/{n} won) — throttled to {EXPLORATION_FLOOR:.0%} while it "
            "keeps proving itself"
        )

    # A positive record carried entirely by one trade is not a record.
    without_best = (sum(results) - max(results)) / (n - 1)
    if without_best <= 0:
        return 0.6, (
            f"{playbook}: ${per_trade:+,.2f}/trade over {n}, but "
            f"${without_best:+,.2f} without its single best — half size"
        )

    return 1.0, (
        f"{playbook}: ${per_trade:+,.2f}/trade over {n} trades "
        f"({wins}/{n} won), holds up without its best — full size"
    )
