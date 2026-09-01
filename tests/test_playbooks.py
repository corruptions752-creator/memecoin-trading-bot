"""Tests for the independent entry playbooks.

The point of several theses is that they fire in different conditions. A
second playbook that agrees with the first everywhere adds correlated risk
and no diversification, so most of what is checked here is that they stay
out of each other's way.
"""

from conftest import deterministic_settings, make_snapshot

from memecoin_bot.playbooks import (
    PLAYBOOKS,
    by_name,
    score_momentum,
    score_reversal,
    score_trend,
)
from memecoin_bot.strategy import decide_entry


def settings():
    return deterministic_settings(min_entry_score=0.35)


def snap(ratio=1.5, **kw):
    """make_snapshot, but with the buy/sell ratio spelled as a ratio.

    The snapshot derives it from raw buy and sell counts; every test here
    cares about the ratio itself.
    """

    return make_snapshot(buys_5m=int(round(ratio * 100)), sells_5m=100, **kw)


# --- the theses do not overlap -------------------------------------------

def test_momentum_and_reversal_never_fire_on_the_same_candle():
    """One wants 5m up, the other wants 5m down. If both ever scored the
    same token the book would hold one thesis twice over."""

    s = settings()
    for change in (-0.40, -0.20, -0.08, 0.10, 0.30, 0.80):
        snapshot = snap(ratio=1.6, price_change_5m=change)
        momentum, _ = score_momentum(snapshot, s)
        reversal, _ = score_reversal(snapshot, s)
        assert not (momentum > 0 and reversal > 0), (
            f"both fired at 5m {change:.0%}"
        )


def test_trend_only_takes_a_quiet_candle():
    """Trend waits for the pause, so it cannot compete with momentum for
    the same pop."""

    s = settings()
    loud = make_snapshot(price_change_5m=0.30, price_change_1h=0.50,
                         price_change_24h=1.0)
    assert score_trend(loud, s)[0] == 0.0

    quiet = make_snapshot(price_change_5m=0.01, price_change_1h=0.50,
                          price_change_24h=1.0)
    assert score_trend(quiet, s)[0] > 0.0


# --- reversal is not a knife-catcher -------------------------------------

def test_reversal_refuses_a_dip_the_sellers_still_own():
    s = settings()
    score, notes = score_reversal(
        snap(ratio=0.6, price_change_5m=-0.20), s
    )
    assert score == 0.0
    assert "sellers still in control" in notes[0]


def test_reversal_refuses_an_outright_collapse():
    s = settings()
    score, notes = score_reversal(
        snap(ratio=2.0, price_change_5m=-0.60), s
    )
    assert score == 0.0
    assert "collapse" in notes[0]


def test_reversal_refuses_a_falling_knife_on_the_hourly():
    s = settings()
    score, notes = score_reversal(
        snap(ratio=1.5, price_change_5m=-0.15,
                      price_change_1h=-0.50), s
    )
    assert score == 0.0
    assert "falling knife" in notes[0]


def test_reversal_takes_a_bought_dip():
    s = settings()
    score, notes = score_reversal(
        snap(ratio=1.8, price_change_5m=-0.20,
                      price_change_1h=0.10), s
    )
    assert score > 0.0
    assert any("into the dip" in n for n in notes)


# --- trend wants a real trend --------------------------------------------

def test_trend_refuses_a_token_going_nowhere():
    s = settings()
    assert score_trend(
        make_snapshot(price_change_5m=0.0, price_change_1h=0.05,
                      price_change_24h=0.1), s
    )[0] == 0.0


def test_trend_refuses_a_red_day_however_good_the_hour():
    """An hour of strength inside a down day is a bounce, not a trend."""

    s = settings()
    assert score_trend(
        make_snapshot(price_change_5m=0.0, price_change_1h=0.60,
                      price_change_24h=-0.30), s
    )[0] == 0.0


# --- selection ------------------------------------------------------------

def test_decide_entry_reports_which_playbook_won():
    s = settings()
    decision = decide_entry(
        snap(ratio=1.8, price_change_5m=-0.20,
                      price_change_1h=0.10), s
    )
    assert decision is not None
    assert decision.strategy == "reversal"


def test_a_playbook_at_its_cap_is_withheld():
    """The engine passes `allowed` to keep one thesis from taking the book."""

    s = settings()
    snapshot = snap(ratio=1.8, price_change_5m=-0.20,
                             price_change_1h=0.10)
    assert decide_entry(snapshot, s, allowed={"reversal"}) is not None
    assert decide_entry(snapshot, s, allowed={"trend"}) is None


def test_every_playbook_leaves_room_for_the_others():
    """No single thesis may hold every slot."""

    for playbook in PLAYBOOKS:
        assert playbook.slot_cap(8) < 8
    assert by_name("momentum") is not None
    assert by_name("nonsense") is None


def test_the_measured_winner_gets_the_largest_share():
    """60 seeds: trend +$5.33/trade, momentum -$2.30. Shares follow the
    evidence, and momentum stays in the book because both real 3x winners
    came through it."""

    shares = {p.name: p.max_share for p in PLAYBOOKS}
    assert shares["trend"] > shares["momentum"] > shares["reversal"]
    assert by_name("momentum") is not None


def test_the_shares_can_oversubscribe_so_no_slot_is_stranded():
    """Caps sum above 1.0 on purpose: if one thesis finds nothing, the
    others must be able to use the idle slots rather than sit in cash."""

    assert sum(p.max_share for p in PLAYBOOKS) > 1.0
