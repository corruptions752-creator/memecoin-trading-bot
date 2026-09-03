"""Tests for sizing a playbook by its own record."""

from memecoin_bot.evidence_gate import (
    EXPLORATION_FLOOR,
    MIN_JUDGEABLE,
    size_multiplier,
)


def rows(strategy, results):
    return [
        {"strategy": strategy, "realized_usd": r, "win": 1 if r > 0 else 0}
        for r in results
    ]


def test_a_short_record_does_not_change_size():
    """Throttling on ten trades is changing the rules after a losing run."""

    multiplier, why = size_multiplier(rows("momentum", [-10] * 10), "momentum")
    assert multiplier == 1.0
    assert "no verdict" in why


def test_a_proven_loser_is_throttled_not_switched_off():
    """Cut to zero and its evidence freezes at its worst moment."""

    multiplier, why = size_multiplier(
        rows("momentum", [-10] * MIN_JUDGEABLE), "momentum"
    )
    assert multiplier == EXPLORATION_FLOOR
    assert 0 < multiplier < 1
    assert "throttled" in why


def test_an_edge_carried_by_one_trade_is_halved():
    """Positive overall, negative without its best: that is the trade, not
    a strategy."""

    results = [-5.0] * (MIN_JUDGEABLE - 1) + [300.0]
    multiplier, why = size_multiplier(rows("trend", results), "trend")
    assert sum(results) / len(results) > 0
    assert multiplier == 0.6
    assert "without its single best" in why


def test_a_genuine_edge_keeps_full_size():
    results = [8.0, -4.0] * (MIN_JUDGEABLE // 2)
    multiplier, why = size_multiplier(rows("trend", results), "trend")
    assert multiplier == 1.0
    assert "holds up without its best" in why


def test_playbooks_are_judged_only_on_their_own_trades():
    mixed = rows("momentum", [-10] * MIN_JUDGEABLE) + rows("trend", [20.0] * 3)
    assert size_multiplier(mixed, "momentum")[0] == EXPLORATION_FLOOR
    assert size_multiplier(mixed, "trend")[0] == 1.0
