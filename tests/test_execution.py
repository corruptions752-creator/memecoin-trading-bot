"""Tests for execution realism.

Each effect modelled here makes results worse. These tests assert that it
actually does, and that it cannot be silently switched off.
"""

from conftest import deterministic_settings

from memecoin_bot.config import Settings
from memecoin_bot.execution import (
    ATA_RENT_LAMPORTS, BASE_FEE_LAMPORTS, ExecutionSimulator, FailureReason,
)

MINT = "Mint111"


def sim(seed=1, **overrides):
    """A simulator with stochastic effects off unless a test enables them."""

    return ExecutionSimulator(deterministic_settings(**overrides), seed=seed)


# --- Costs ---------------------------------------------------------------

def test_a_successful_buy_charges_network_and_pool_fees():
    result = sim().buy(MINT, 100.0, 0.001, 100_000.0)
    assert result.succeeded
    assert result.network_fee_usd > 0
    assert result.pool_fee_usd > 0


def test_the_first_buy_of_a_token_pays_account_rent():
    """Creating an associated token account costs rent that is locked up."""

    simulator = sim()
    first = simulator.buy(MINT, 100.0, 0.001, 100_000.0)
    second = simulator.buy(MINT, 100.0, 0.001, 100_000.0)
    assert first.rent_usd > 0
    assert second.rent_usd == 0


def test_rent_matches_the_real_lamport_cost():
    settings = deterministic_settings(sol_price_usd=150.0)
    result = ExecutionSimulator(settings, seed=1).buy(
        MINT, 100.0, 0.001, 100_000.0
    )
    expected = (ATA_RENT_LAMPORTS / 1_000_000_000) * 150.0
    assert abs(result.rent_usd - expected) < 1e-9


def test_the_base_fee_is_included():
    settings = deterministic_settings(
        sol_price_usd=150.0, priority_fee_lamports=0.0
    )
    result = ExecutionSimulator(settings, seed=1).buy(
        MINT, 100.0, 0.001, 100_000.0
    )
    expected = (BASE_FEE_LAMPORTS / 1_000_000_000) * 150.0
    assert abs(result.network_fee_usd - expected) < 1e-9


def test_a_round_trip_loses_money_at_an_unchanged_price():
    simulator = sim()
    buy = simulator.buy(MINT, 100.0, 0.001, 100_000.0)
    sell = simulator.sell(MINT, buy.tokens, 0.001, 100_000.0)
    assert sell.usd - sell.total_cost_usd < 100.0


# --- Price impact --------------------------------------------------------

def test_impact_is_computed_from_the_pool_not_a_constant():
    thin = sim().buy(MINT, 1_000.0, 0.001, 25_000.0)
    deep = sim().buy(MINT, 1_000.0, 0.001, 5_000_000.0)
    assert thin.price_impact > deep.price_impact * 5


def test_a_buy_fills_above_the_quoted_price():
    result = sim().buy(MINT, 100.0, 0.001, 100_000.0)
    assert result.effective_price_usd > 0.001


def test_a_sell_fills_below_the_quoted_price():
    result = sim().sell(MINT, 100_000.0, 0.001, 100_000.0)
    assert result.effective_price_usd < 0.001


# --- Failures ------------------------------------------------------------

def test_excessive_slippage_fails_the_transaction():
    settings = deterministic_settings(max_slippage_pct=0.01)
    result = ExecutionSimulator(settings, seed=1).buy(
        MINT, 5_000.0, 0.001, 25_000.0
    )
    assert not result.succeeded
    assert result.failure is FailureReason.SLIPPAGE_EXCEEDED


def test_a_landed_but_reverted_transaction_still_burns_fees():
    """This is why failures matter: they are not free."""

    settings = deterministic_settings(max_slippage_pct=0.01)
    result = ExecutionSimulator(settings, seed=1).buy(
        MINT, 5_000.0, 0.001, 25_000.0
    )
    assert not result.succeeded
    assert result.network_fee_usd > 0


def test_a_dropped_transaction_costs_nothing():
    """A transaction that never lands pays no fee."""

    settings = deterministic_settings(tx_drop_rate=1.0)
    result = ExecutionSimulator(settings, seed=1).buy(
        MINT, 100.0, 0.001, 100_000.0
    )
    assert not result.succeeded
    assert result.failure is FailureReason.CONGESTION_DROPPED
    assert result.network_fee_usd == 0.0


def test_transactions_drop_at_the_configured_rate():
    settings = deterministic_settings(tx_drop_rate=0.30)
    simulator = ExecutionSimulator(settings, seed=7)
    outcomes = [
        simulator.buy(MINT, 50.0, 0.001, 500_000.0).failure
        for _ in range(400)
    ]
    dropped = sum(1 for f in outcomes if f is FailureReason.CONGESTION_DROPPED)
    assert 0.22 < dropped / 400 < 0.38


def test_an_empty_pool_fails_with_no_liquidity():
    result = sim().buy(MINT, 100.0, 0.001, 0.0)
    assert not result.succeeded
    assert result.failure is FailureReason.NO_LIQUIDITY


def test_urgent_sells_tolerate_worse_prices():
    """A stop-loss must get out even at a bad price; not exiting is worse."""

    settings = deterministic_settings(
        max_slippage_pct=0.02, urgent_slippage_pct=0.60
    )
    ordinary = ExecutionSimulator(settings, seed=1).sell(
        MINT, 2_000_000.0, 0.001, 25_000.0, urgent=False
    )
    urgent = ExecutionSimulator(settings, seed=1).sell(
        MINT, 2_000_000.0, 0.001, 25_000.0, urgent=True
    )
    assert not ordinary.succeeded
    assert urgent.succeeded


# --- Latency and adverse selection ---------------------------------------

def test_latency_is_positive_when_configured():
    settings = Settings(tx_drop_rate=0.0, sandwich_base_rate=0.0)
    result = ExecutionSimulator(settings, seed=3).buy(
        MINT, 100.0, 0.001, 500_000.0
    )
    assert result.latency_seconds > 0


def test_drift_is_adverse_on_average():
    """The bot buys strength, so the in-flight move tends to go against it."""

    settings = Settings(
        tx_drop_rate=0.0, sandwich_base_rate=0.0, adverse_selection_bps=40.0,
    )
    simulator = ExecutionSimulator(settings, seed=11)
    drifts = [
        simulator.buy(MINT, 50.0, 0.001, 500_000.0).drift for _ in range(300)
    ]
    assert sum(drifts) / len(drifts) < 0


def test_a_volatile_token_drifts_further():
    settings = Settings(tx_drop_rate=0.0, sandwich_base_rate=0.0)

    def spread(volatility):
        simulator = ExecutionSimulator(settings, seed=5)
        values = [
            simulator.buy(
                MINT, 50.0, 0.001, 500_000.0, volatility=volatility
            ).drift
            for _ in range(200)
        ]
        mean = sum(values) / len(values)
        return sum((v - mean) ** 2 for v in values) / len(values)

    assert spread(0.05) > spread(0.001)


# --- Sandwiching ---------------------------------------------------------

def test_a_sandwich_worsens_the_fill():
    settings = deterministic_settings(sandwich_base_rate=1.0)
    attacked = ExecutionSimulator(settings, seed=1).buy(
        MINT, 500.0, 0.001, 100_000.0
    )
    clean = sim().buy(MINT, 500.0, 0.001, 100_000.0)
    assert attacked.sandwiched
    assert attacked.effective_price_usd > clean.effective_price_usd


def test_sandwich_risk_scales_with_order_size():
    settings = deterministic_settings(sandwich_base_rate=0.25)

    def hit_rate(usd):
        simulator = ExecutionSimulator(settings, seed=13)
        return sum(
            simulator.buy(MINT, usd, 0.001, 100_000.0).sandwiched
            for _ in range(300)
        ) / 300

    assert hit_rate(3_000.0) > hit_rate(50.0)


def test_sells_are_not_sandwiched():
    """Attackers profit by front-running buys, not exits."""

    settings = deterministic_settings(sandwich_base_rate=1.0)
    result = ExecutionSimulator(settings, seed=1).sell(
        MINT, 100_000.0, 0.001, 100_000.0
    )
    assert not result.sandwiched


# --- Determinism ---------------------------------------------------------

def test_the_same_seed_reproduces_the_same_outcome():
    a = ExecutionSimulator(Settings(), seed=42).buy(MINT, 100.0, 0.001, 100_000.0)
    b = ExecutionSimulator(Settings(), seed=42).buy(MINT, 100.0, 0.001, 100_000.0)
    assert a.effective_price_usd == b.effective_price_usd
    assert a.latency_seconds == b.latency_seconds


# --- Account rent is a deposit, not a fee --------------------------------

def test_rent_is_refunded_when_the_account_is_closed():
    """Rent is reclaimable. Charging it as a permanent fee overstated costs
    by roughly 5x at a 1% position size."""

    simulator = sim()
    buy = simulator.buy(MINT, 100.0, 0.001, 100_000.0)
    sell = simulator.sell(
        MINT, buy.tokens, 0.001, 100_000.0, close_account=True
    )
    assert buy.rent_usd > 0
    assert abs(sell.rent_refund_usd - buy.rent_usd) < 1e-9


def test_a_partial_exit_does_not_refund_rent():
    """The account is still needed while any of the position is held."""

    simulator = sim()
    buy = simulator.buy(MINT, 100.0, 0.001, 100_000.0)
    sell = simulator.sell(
        MINT, buy.tokens / 2, 0.001, 100_000.0, close_account=False
    )
    assert sell.rent_refund_usd == 0.0


def test_closing_an_account_costs_one_more_transaction_fee():
    simulator = sim()
    buy = simulator.buy(MINT, 100.0, 0.001, 100_000.0)
    plain = sim().sell(MINT, buy.tokens, 0.001, 100_000.0, close_account=False)

    simulator2 = sim()
    buy2 = simulator2.buy(MINT, 100.0, 0.001, 100_000.0)
    closing = simulator2.sell(
        MINT, buy2.tokens, 0.001, 100_000.0, close_account=True
    )
    assert closing.network_fee_usd > plain.network_fee_usd


def test_rent_cannot_be_refunded_twice():
    simulator = sim()
    buy = simulator.buy(MINT, 100.0, 0.001, 100_000.0)
    first = simulator.sell(
        MINT, buy.tokens / 2, 0.001, 100_000.0, close_account=True
    )
    second = simulator.sell(
        MINT, buy.tokens / 2, 0.001, 100_000.0, close_account=True
    )
    assert first.rent_refund_usd > 0
    assert second.rent_refund_usd == 0.0


def test_a_full_round_trip_nets_out_rent():
    """Rent must contribute exactly nothing to the cost of a closed trade.

    The remaining cost is fees and price impact, which on a $100 trade at
    25bps exceeds the rent deposit -- so the property to assert is that rent
    cancels, not that total cost is small.
    """

    simulator = sim()
    buy = simulator.buy(MINT, 100.0, 0.001, 100_000.0)
    sell = simulator.sell(
        MINT, buy.tokens, 0.001, 100_000.0, close_account=True
    )
    assert abs(buy.rent_usd - sell.rent_refund_usd) < 1e-12

    # What is left is fees and impact only.
    total = buy.total_cost_usd + sell.total_cost_usd
    fees_only = (
        buy.network_fee_usd + buy.pool_fee_usd
        + sell.network_fee_usd + sell.pool_fee_usd
    )
    assert abs(total - fees_only) < 1e-12
