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


COHORT_MIN = 10
"""Matched past setups needed before a cohort's record can authorise a trade."""


def earnings_only_verdict(
    records: "list", playbook: str, neighbours: "list",
) -> tuple[bool, str]:
    """Decide whether this setup has *demonstrated* earnings behind it.

    Earnings-only mode inverts the usual burden. Normally a trade is taken
    unless something objects; here it is refused unless the record shows
    that setups like this one have actually made money. Two ways to clear:
    the playbook's own record is positive over a judgeable sample, or the
    matched cohort of similar past setups is.

    This will often authorise nothing. That is the mode working, not
    failing -- an empty book is the correct output when no cohort has
    demonstrated an edge, and it is the whole reason to run it.
    """

    mine = [r for r in records if r["strategy"] == playbook]
    if len(mine) >= MIN_JUDGEABLE:
        results = [r["realized_usd"] or 0.0 for r in mine]
        per_trade = sum(results) / len(results)
        if per_trade > 0:
            without_best = (sum(results) - max(results)) / (len(results) - 1)
            if without_best > 0:
                return True, (
                    f"{playbook} earns ${per_trade:+,.2f}/trade over "
                    f"{len(mine)} and holds up without its best"
                )
            return False, (
                f"{playbook} is positive only because of one trade "
                f"(${without_best:+,.2f} without it)"
            )
        # Judgeable and losing. Say so here rather than falling through to
        # the generic "not enough data" line, which reads as though the
        # record were missing when in fact the record is the reason.
        return False, (
            f"{playbook} loses ${abs(per_trade):,.2f}/trade over "
            f"{len(mine)} trades"
        )

    if len(neighbours) >= COHORT_MIN:
        results = [r["realized_usd"] or 0.0 for r in neighbours]
        per_trade = sum(results) / len(results)
        wins = sum(1 for r in neighbours if r["win"])
        if per_trade > 0:
            return True, (
                f"matched cohort of {len(neighbours)} earns "
                f"${per_trade:+,.2f}/trade ({wins} won)"
            )
        return False, (
            f"matched cohort of {len(neighbours)} loses "
            f"${abs(per_trade):,.2f}/trade"
        )

    return False, (
        f"no demonstrated earnings: {playbook} has {len(mine)} trades and "
        f"only {len(neighbours)} comparable setups"
    )
