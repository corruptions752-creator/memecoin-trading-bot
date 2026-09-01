"""Tests for the analyst panel.

Most of these guard the same failure: a panel that manufactures confidence.
Agreement between agents reading one signal is not evidence, a win rate
from six trades is not a win rate, and a missing data source is not a
neutral opinion.
"""

import pytest
from conftest import deterministic_settings, make_snapshot

from memecoin_bot.agents import AnalysisContext, Decision, ROSTER, run_panel
from memecoin_bot.agents.base import (
    AgentReport,
    SignalFamily,
    Stance,
    insufficient,
)
from memecoin_bot.agents.evidence import AdversarialAgent, HistoricalPatternAgent
from memecoin_bot.agents.market import LiquidityRiskAgent, SentimentAgent
from memecoin_bot.regime import Regime, detect
from memecoin_bot.statistics_tools import mean_without_best, wilson_interval
from memecoin_bot.trade_memory import SetupFeatures, agent_accuracy, distance


def context(**kw):
    base = dict(
        snapshot=make_snapshot(), settings=deterministic_settings(),
        market=(), history=(), open_positions=(), regime="sideways",
        position_size_usd=50.0,
    )
    base.update(kw)
    ctx = AnalysisContext(**base)
    ctx.proposed_strategy = kw.pop("strategy", "momentum")
    return ctx


def closed_setup(win=True, **kw):
    """A minimal closed trade_features row as a dict."""

    row = dict(
        momentum_5m=0.10, momentum_1h=0.15, momentum_24h=0.4, turnover=5.0,
        buy_sell_ratio=1.5, liquidity_usd=100_000.0, volume_24h_usd=500_000.0,
        fdv_usd=2_000_000.0, pair_age_hours=6.0, strategy="momentum",
        regime="sideways", realized_usd=20.0 if win else -15.0,
        win=1 if win else 0, reports="{}",
    )
    row.update(kw)
    return row


# --- the report contract refuses to lie ----------------------------------

def test_insufficient_data_cannot_carry_confidence():
    """Otherwise "I don't know, 70/100" becomes expressible."""

    with pytest.raises(ValueError, match="insufficient data"):
        AgentReport(
            agent="x", stance=Stance.INSUFFICIENT_DATA, confidence=70.0,
            signal_family=SignalFamily.HISTORY,
        )


def test_confidence_outside_the_scale_is_refused():
    with pytest.raises(ValueError, match="outside 0-100"):
        AgentReport(
            agent="x", stance=Stance.BULLISH, confidence=140.0,
            signal_family=SignalFamily.FLOW,
        )


# --- the honest no-answer -------------------------------------------------

def test_sentiment_admits_it_has_no_feed():
    """There is no social source wired to this bot. Inventing a sentiment
    score from trade counts would double-count the momentum agent."""

    report = SentimentAgent().analyse(context())
    assert report.stance is Stance.INSUFFICIENT_DATA
    assert report.confidence == 0.0


def test_history_refuses_to_read_a_handful_of_setups():
    report = HistoricalPatternAgent().analyse(
        context(history=tuple(closed_setup() for _ in range(4)))
    )
    assert report.stance is Stance.INSUFFICIENT_DATA
    assert "too few" in report.notes[0]


def test_history_reports_the_interval_not_just_the_rate():
    history = tuple(closed_setup(win=i % 3 != 0) for i in range(40))
    report = HistoricalPatternAgent().analyse(context(history=history))
    assert report.evidence_n == 40
    assert any("interval" in n for n in report.notes)


def test_a_losing_neighbourhood_turns_the_agent_bearish():
    """The setup can look fine on the tape and still be refused."""

    history = tuple(closed_setup(win=False) for _ in range(40))
    report = HistoricalPatternAgent().analyse(context(history=history))
    assert report.stance is Stance.BEARISH


# --- the risk agent can stop a trade -------------------------------------

def test_liquidity_vetoes_a_position_too_big_for_the_pool():
    report = LiquidityRiskAgent().analyse(context(
        snapshot=make_snapshot(liquidity_usd=500.0), position_size_usd=50.0,
    ))
    assert report.stance is Stance.VETO


def test_a_veto_rejects_however_bullish_everything_else_is():
    verdict = run_panel(context(
        snapshot=make_snapshot(
            liquidity_usd=400.0, price_change_5m=0.20, price_change_1h=0.40,
            buys_5m=400, sells_5m=50,
        ),
        position_size_usd=50.0,
    ))
    assert verdict.decision is Decision.REJECT
    assert verdict.vetoed_by == "liquidity"


# --- the anti-echo-chamber mechanism -------------------------------------

def test_the_adversary_catches_one_signal_wearing_several_hats():
    ctx = context()
    for name in ("a", "b", "c"):
        ctx.reports[name] = AgentReport(
            agent=name, stance=Stance.BULLISH, confidence=85.0,
            signal_family=SignalFamily.PRICE_ACTION,
        )
    report = AdversarialAgent().analyse(ctx)
    assert report.stance is Stance.BEARISH
    assert any("one signal, not three" in f for f in report.factors_against)


def test_agreement_within_one_family_does_not_raise_confidence():
    """Two price-action agents saying the same thing is one opinion.

    The lead collapses a family to its strongest voice, so a duplicate that
    does not outrank the incumbent must not move the number at all. (A
    *more* confident report legitimately becomes the family's voice -- that
    is the mechanism working, not an echo.)
    """

    strong = make_snapshot(price_change_5m=0.15, price_change_1h=0.30,
                           buys_5m=200, sells_5m=80, liquidity_usd=150_000)
    # Enough history that the no-evidence cap is not what holds the number
    # down. Without this the cap masks the mechanism and the test passes
    # even when family collapsing is removed -- which it did, once.
    history = tuple(closed_setup(win=i % 2 == 0) for i in range(60))
    one = run_panel(context(snapshot=strong, history=history)).confidence

    class Echo:
        name = "echo"
        signal_family = SignalFamily.PRICE_ACTION

        def analyse(self, ctx):
            # Below the technical agent's score, so it adds no new
            # information -- only another voice saying the same thing.
            return AgentReport(
                agent="echo", stance=Stance.BULLISH, confidence=60.0,
                signal_family=SignalFamily.PRICE_ACTION,
            )

    two = run_panel(
        context(snapshot=strong, history=history), roster=(*ROSTER, Echo())
    ).confidence
    assert two == pytest.approx(one), "a duplicate signal moved confidence"


# --- confidence has to be earned -----------------------------------------

def test_no_history_caps_confidence_however_good_the_tape():
    verdict = run_panel(context(snapshot=make_snapshot(
        price_change_5m=0.20, price_change_1h=0.45, price_change_24h=1.0,
        buys_5m=400, sells_5m=60, liquidity_usd=250_000,
        volume_24h_usd=3_000_000,
    )))
    assert verdict.sample_size == 0
    assert verdict.confidence <= 55.0
    assert verdict.decision is not Decision.PAPER_TRADE or verdict.size_multiplier < 1.0


def test_high_conviction_needs_a_real_sample():
    """1.5x size is reserved for setups with a measured history behind them."""

    thin = run_panel(context(
        snapshot=make_snapshot(price_change_5m=0.15, buys_5m=300, sells_5m=60),
        history=tuple(closed_setup() for _ in range(6)),
    ))
    assert thin.size_multiplier < 1.5


# --- a crashing agent must not stop the cycle ----------------------------

def test_a_broken_agent_is_recorded_as_having_no_opinion():
    class Broken:
        name = "broken"
        signal_family = SignalFamily.FLOW

        def analyse(self, ctx):
            raise RuntimeError("boom")

    verdict = run_panel(context(), roster=(*ROSTER, Broken()))
    assert verdict.reports["broken"].stance is Stance.INSUFFICIENT_DATA
    assert "boom" in verdict.reports["broken"].notes[0]


# --- regime ---------------------------------------------------------------

def test_regime_is_unknown_when_breadth_is_too_thin():
    assert detect([make_snapshot() for _ in range(3)])[0] is Regime.UNKNOWN


def test_a_violent_tape_is_not_filed_as_a_bull_market():
    """Most things green plus huge candles is high volatility, not a bull
    run; the dangerous read has to win."""

    wild = [make_snapshot(price_change_5m=0.35, price_change_1h=0.40)
            for _ in range(12)]
    assert detect(wild)[0] is Regime.HIGH_VOLATILITY


def test_thin_liquidity_outranks_green_breadth():
    thin = [make_snapshot(liquidity_usd=5_000, price_change_1h=0.30,
                          price_change_5m=0.02) for _ in range(12)]
    assert detect(thin)[0] is Regime.LOW_LIQUIDITY


# --- statistics -----------------------------------------------------------

def test_a_small_sample_carries_a_wide_interval():
    """8/10 reads as 80% but says almost nothing."""

    _, low, high = wilson_interval(8, 10)
    _, low_big, high_big = wilson_interval(480, 600)
    assert (high - low) > 3 * (high_big - low_big)


def test_mean_without_best_exposes_a_one_seed_edge():
    carried = [-10, -10, -10, -10, 100]
    assert sum(carried) / len(carried) > 0
    assert mean_without_best(carried) < 0


# --- memory ---------------------------------------------------------------

def test_similar_setups_are_near_and_different_ones_are_far():
    here = SetupFeatures(momentum_5m=0.10, turnover=3.0, buy_sell_ratio=1.5,
                         liquidity_usd=50_000)
    near = SetupFeatures(momentum_5m=0.11, turnover=3.2, buy_sell_ratio=1.4,
                         liquidity_usd=55_000)
    far = SetupFeatures(momentum_5m=-0.30, turnover=0.2, buy_sell_ratio=0.4,
                        liquidity_usd=8_000)
    assert distance(here, near) < distance(here, far)


def test_agent_accuracy_ignores_the_calls_an_agent_declined_to_make():
    rows = [
        {"reports": '{"a":["bullish",80],"b":["neutral",50]}', "win": 1},
        {"reports": '{"a":["bullish",80],"b":["neutral",50]}', "win": 0},
    ]
    scored = agent_accuracy(rows)
    assert scored["a"]["calls"] == 2
    assert scored["a"]["accuracy"] == 0.5
    assert "b" not in scored, "a neutral report was scored as a prediction"


def test_an_always_bearish_agent_does_not_score_as_skilled():
    """When most trades lose, saying "bearish" every time is right most of
    the time while carrying no information. Scored against one overall base
    rate this agent came out as the best forecaster on the panel; lift is
    measured per direction so it comes out at zero, which is the truth.
    """

    # 8 of 10 lose. A permanent bear is 80% "accurate".
    rows = [
        {"reports": '{"perma_bear":["bearish",70]}', "win": 1 if i < 2 else 0}
        for i in range(10)
    ]
    scored = agent_accuracy(rows)["perma_bear"]

    assert scored["accuracy"] == pytest.approx(0.8)
    assert scored["lift"] == pytest.approx(0.0, abs=0.01), (
        "an agent with one opinion scored as informative"
    )


def test_a_genuinely_informative_agent_shows_positive_lift():
    """Same base rate, but this agent's bullish calls actually pick winners."""

    rows = []
    for i in range(10):
        won = i < 2
        stance = "bullish" if won else "bearish"
        rows.append({"reports": f'{{"good":["{stance}",70]}}', "win": 1 if won else 0})

    assert agent_accuracy(rows)["good"]["lift"] > 0.2
