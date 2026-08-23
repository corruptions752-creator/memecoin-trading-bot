"""Tests for constant-product AMM math.

These check the math against properties that must hold for x*y=k, not against
numbers copied out of the implementation.
"""

from memecoin_bot.amm import (
    PoolState, buy_output, pool_from_snapshot, sell_output,
)


def test_reserves_reproduce_the_quoted_price():
    pool = pool_from_snapshot(100_000.0, 0.002)
    assert abs(pool.spot_price_usd - 0.002) < 1e-12


def test_liquidity_is_split_across_both_sides():
    pool = pool_from_snapshot(100_000.0, 0.002)
    assert pool.quote_reserve_usd == 50_000.0


def test_a_buy_always_pays_above_spot():
    pool = pool_from_snapshot(100_000.0, 0.001)
    tokens, impact = buy_output(pool, 500.0)
    realized = 500.0 / tokens
    assert realized > pool.spot_price_usd
    assert impact > 0


def test_a_sell_always_receives_below_spot():
    pool = pool_from_snapshot(100_000.0, 0.001)
    usd, impact = sell_output(pool, 500_000.0)
    realized = usd / 500_000.0
    assert realized < pool.spot_price_usd
    assert impact > 0


def test_impact_grows_with_size():
    pool = pool_from_snapshot(100_000.0, 0.001)
    _, small = buy_output(pool, 10.0)
    _, medium = buy_output(pool, 500.0)
    _, large = buy_output(pool, 5_000.0)
    assert small < medium < large


def test_impact_shrinks_with_depth():
    _, thin = buy_output(pool_from_snapshot(25_000.0, 0.001), 500.0)
    _, deep = buy_output(pool_from_snapshot(5_000_000.0, 0.001), 500.0)
    assert deep < thin


def test_a_thin_pool_is_far_worse_than_a_flat_estimate():
    """The old flat 1.5% model understated thin-pool cost by roughly 5x.

    This is the case that matters: it is where the strategy actually trades.
    """

    pool = pool_from_snapshot(25_000.0, 0.001)
    _, impact = buy_output(pool, 1_000.0)
    assert impact > 0.05


def test_a_deep_pool_is_far_better_than_a_flat_estimate():
    pool = pool_from_snapshot(1_000_000.0, 0.001)
    _, impact = buy_output(pool, 10.0)
    assert impact < 0.005


def test_the_constant_product_is_preserved_net_of_fees():
    """Reserves after a swap must satisfy x*y >= k, fees growing k."""

    pool = pool_from_snapshot(100_000.0, 0.001)
    usd_in = 1_000.0
    tokens, _ = buy_output(pool, usd_in)
    after = PoolState(
        base_reserve=pool.base_reserve - tokens,
        quote_reserve_usd=pool.quote_reserve_usd + usd_in,
    )
    assert after.k >= pool.k


def test_a_round_trip_loses_money_at_an_unchanged_price():
    """Buy then immediately sell must lose: impact both ways plus fees."""

    pool = pool_from_snapshot(100_000.0, 0.001)
    tokens, _ = buy_output(pool, 1_000.0)
    back, _ = sell_output(pool, tokens)
    assert back < 1_000.0


def test_zero_and_negative_sizes_are_rejected():
    pool = pool_from_snapshot(100_000.0, 0.001)
    assert buy_output(pool, 0.0) == (0.0, 0.0)
    assert sell_output(pool, -5.0) == (0.0, 0.0)


def test_an_invalid_pool_is_refused():
    assert pool_from_snapshot(0.0, 0.001) is None
    assert pool_from_snapshot(100_000.0, 0.0) is None
    assert pool_from_snapshot(-1.0, 0.001) is None


def test_higher_pool_fees_cost_more():
    pool = pool_from_snapshot(100_000.0, 0.001)
    cheap, _ = buy_output(pool, 1_000.0, fee_bps=25.0)
    dear, _ = buy_output(pool, 1_000.0, fee_bps=100.0)
    assert dear < cheap


def test_selling_more_than_the_pool_holds_still_returns_finite_usd():
    """Constant product never fully drains; it just gets ruinous."""

    pool = pool_from_snapshot(100_000.0, 0.001)
    usd, impact = sell_output(pool, pool.base_reserve * 100)
    assert 0 < usd < pool.quote_reserve_usd
    assert impact > 0.9
