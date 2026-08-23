"""Tests for simulated execution and the live-mode guard."""

import pytest
from conftest import deterministic_settings, make_snapshot

from memecoin_bot.broker import (
    ExecutionFailed, LiveBroker, PaperBroker, build_broker,
)
from memecoin_bot.config import LIVE, PAPER, Settings
from memecoin_bot.models import Side


def test_buy_pays_above_the_quoted_price(settings):
    """Slippage must work against the trader, never for them."""

    broker = PaperBroker(deterministic_settings(), seed=1)
    snapshot = make_snapshot(price_usd=0.001)
    fill = broker.buy(snapshot, 100.0)

    assert fill.side is Side.BUY
    assert fill.price_usd > snapshot.price_usd
    assert fill.quantity < 100.0 / snapshot.price_usd


def test_sell_receives_below_the_quoted_price(settings):
    broker = PaperBroker(deterministic_settings(), seed=1)
    snapshot = make_snapshot(price_usd=0.001)
    fill = broker.sell(snapshot, 100_000.0)

    assert fill.side is Side.SELL
    assert fill.price_usd < snapshot.price_usd


def test_costs_are_charged_on_both_legs(settings):
    """A round trip at an unchanged price must lose money."""

    broker = PaperBroker(deterministic_settings(), seed=1)
    snapshot = make_snapshot(price_usd=0.001)

    buy = broker.buy(snapshot, 100.0)
    sell = broker.sell(snapshot, buy.quantity)

    assert buy.net_usd > 100.0          # paid more than notional
    assert sell.net_usd < 100.0          # received less than notional
    assert sell.net_usd < buy.net_usd


def test_slippage_grows_with_size_relative_to_the_pool(settings):
    broker = PaperBroker(deterministic_settings(), seed=1)
    shallow = make_snapshot(liquidity_usd=20_000.0)

    small = broker.buy(shallow, 10.0)
    large = broker.buy(shallow, 5_000.0)

    small_bps = (small.price_usd / shallow.price_usd - 1) * 10_000
    large_bps = (large.price_usd / shallow.price_usd - 1) * 10_000
    assert large_bps > small_bps


def test_an_impossible_trade_fails_rather_than_filling(settings):
    """A trade far larger than the pool cannot fill at any sane price.

    The old model silently filled it at a capped slippage. Constant-product
    math says the price runs away, so the transaction fails on slippage --
    which is what happens on chain.
    """

    broker = PaperBroker(Settings(max_slippage_pct=0.05), seed=1)
    with pytest.raises(ExecutionFailed) as caught:
        broker.buy(make_snapshot(liquidity_usd=100.0), 1_000_000.0)
    assert caught.value.result.failure is not None


def test_an_empty_pool_fails_without_crashing(settings):
    broker = PaperBroker(deterministic_settings(), seed=1)
    with pytest.raises(ExecutionFailed):
        broker.buy(make_snapshot(liquidity_usd=0.0), 10.0)


def test_selling_into_a_dead_feed_yields_nothing(settings):
    """A worthless position must book zero, not raise."""

    broker = PaperBroker(deterministic_settings(), seed=1)
    fill = broker.sell(make_snapshot(price_usd=0.0), 10_000.0)
    assert fill.gross_usd == 0.0
    assert fill.net_usd == 0.0


def test_fills_are_recorded(settings):
    broker = PaperBroker(deterministic_settings(), seed=1)
    broker.buy(make_snapshot(), 10.0)
    broker.sell(make_snapshot(), 100.0)
    assert len(broker.fills) == 2


def test_non_positive_sizes_are_rejected(settings):
    broker = PaperBroker(deterministic_settings(), seed=1)
    with pytest.raises(ValueError):
        broker.buy(make_snapshot(), 0.0)
    with pytest.raises(ValueError):
        broker.sell(make_snapshot(), -1.0)


def test_buying_without_a_price_is_rejected(settings):
    broker = PaperBroker(deterministic_settings(), seed=1)
    with pytest.raises(ValueError):
        broker.buy(make_snapshot(price_usd=0.0), 10.0)


def test_paper_mode_builds_a_paper_broker():
    assert isinstance(build_broker(Settings(mode=PAPER)), PaperBroker)


def test_live_mode_builds_a_live_broker():
    assert isinstance(build_broker(Settings(mode=LIVE)), LiveBroker)


def test_live_broker_refuses_to_trade_rather_than_no_op():
    """An unimplemented live path must be loud, never silently inert."""

    broker = LiveBroker(Settings(mode=LIVE))
    with pytest.raises(NotImplementedError, match="not implemented"):
        broker.buy(make_snapshot(), 10.0)
    with pytest.raises(NotImplementedError):
        broker.sell(make_snapshot(), 10.0)
