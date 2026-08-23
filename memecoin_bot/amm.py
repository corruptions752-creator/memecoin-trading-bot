"""Constant-product AMM math.

Solana meme coin pools (Raydium, Orca, Meteora) are constant-product markets:
``x * y = k``. Price impact is therefore not a tunable constant -- it is
determined exactly by trade size relative to the reserve being paid into.

Modelling this properly is the difference between a paper result that
transfers to live trading and one that does not. A 1% position in a $30k pool
moves the price several percent before fees; a linear slippage estimate hides
that entirely, and hides it worst exactly where it matters most: on the small,
thin pools this strategy trades.
"""

from dataclasses import dataclass

# Typical AMM swap fee, taken by the pool on the input amount. Raydium and
# Orca standard pools sit at 0.25%; concentrated-liquidity pools vary.
DEFAULT_POOL_FEE_BPS = 25.0


@dataclass(frozen=True, slots=True)
class PoolState:
    """Reserves of a two-sided constant-product pool.

    ``base`` is the meme coin, ``quote`` is SOL expressed in USD so that the
    whole engine can work in one unit.
    """

    base_reserve: float
    quote_reserve_usd: float

    @property
    def spot_price_usd(self) -> float:
        """Marginal price before any trade."""

        if self.base_reserve <= 0:
            return 0.0
        return self.quote_reserve_usd / self.base_reserve

    @property
    def k(self) -> float:
        """The constant product."""

        return self.base_reserve * self.quote_reserve_usd


def pool_from_snapshot(liquidity_usd: float, price_usd: float) -> PoolState | None:
    """Infer reserves from reported liquidity and price.

    Feeds report total pool value across both sides, so each side holds
    roughly half. This is an approximation -- concentrated liquidity pools
    concentrate depth around the current price and behave better than this
    near spot, and far worse outside the active range -- but it is far closer
    to reality than assuming a fixed slippage percentage, and it errs toward
    pessimism, which is the right direction for a trading simulation.
    """

    if liquidity_usd <= 0 or price_usd <= 0:
        return None

    quote_usd = liquidity_usd / 2.0
    base = quote_usd / price_usd
    if base <= 0:
        return None
    return PoolState(base_reserve=base, quote_reserve_usd=quote_usd)


def buy_output(
    pool: PoolState, usd_in: float, fee_bps: float = DEFAULT_POOL_FEE_BPS
) -> tuple[float, float]:
    """Tokens received for ``usd_in``, and the fraction of price impact.

    Solves the constant product exactly::

        dx = x * dy_after_fee / (y + dy_after_fee)

    Returns ``(tokens_out, price_impact_fraction)`` where impact is the
    shortfall of the realized average price against spot.
    """

    if usd_in <= 0 or pool.base_reserve <= 0 or pool.quote_reserve_usd <= 0:
        return 0.0, 0.0

    fee = usd_in * fee_bps / 10_000.0
    effective_in = usd_in - fee

    tokens_out = (
        pool.base_reserve * effective_in / (pool.quote_reserve_usd + effective_in)
    )
    if tokens_out <= 0:
        return 0.0, 0.0

    spot = pool.spot_price_usd
    realized = usd_in / tokens_out
    impact = (realized - spot) / spot if spot > 0 else 0.0
    return tokens_out, max(0.0, impact)


def sell_output(
    pool: PoolState, tokens_in: float, fee_bps: float = DEFAULT_POOL_FEE_BPS
) -> tuple[float, float]:
    """USD received for ``tokens_in``, and the fraction of price impact.

    Selling into a thin pool is where meme coin positions actually die: the
    same size that cost 2% to enter can cost far more to exit once the pool
    has thinned, which is precisely the case the liquidity-collapse exit is
    trying to escape.
    """

    if tokens_in <= 0 or pool.base_reserve <= 0 or pool.quote_reserve_usd <= 0:
        return 0.0, 0.0

    fee_multiplier = 1.0 - fee_bps / 10_000.0
    effective_in = tokens_in * fee_multiplier

    usd_out = (
        pool.quote_reserve_usd * effective_in / (pool.base_reserve + effective_in)
    )
    if usd_out <= 0:
        return 0.0, 0.0

    spot = pool.spot_price_usd
    realized = usd_out / tokens_in
    impact = (spot - realized) / spot if spot > 0 else 0.0
    return usd_out, max(0.0, impact)


def apply_trade(pool: PoolState, base_delta: float, quote_delta_usd: float) -> PoolState:
    """Return the pool after reserves change by the given deltas.

    Used to model a sandwich attacker's front-run moving the pool before our
    own order executes against it.
    """

    return PoolState(
        base_reserve=max(1e-18, pool.base_reserve + base_delta),
        quote_reserve_usd=max(1e-18, pool.quote_reserve_usd + quote_delta_usd),
    )


def price_impact_for_usd(pool: PoolState, usd_in: float) -> float:
    """Price impact of buying ``usd_in``, as a fraction. Convenience wrapper."""

    _, impact = buy_output(pool, usd_in)
    return impact
