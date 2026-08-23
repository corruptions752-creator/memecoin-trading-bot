"""Realistic execution simulation.

A paper fill that assumes an instant, free, always-successful trade at the
quoted price teaches nothing. Everything that separates a backtest from a live
result happens between the decision and the confirmation, and all of it costs
money:

* **Latency.** A Solana swap takes roughly a second to land. Meme coins move
  in that second, and they move against you more often than not, because the
  same momentum that triggered the entry is what other bots are also chasing.
* **Price impact.** Real constant-product math, not a flat percentage. See
  :mod:`memecoin_bot.amm`.
* **Sandwich attacks.** A profitable buy in a thin pool is a target. The
  attacker buys ahead of you, you fill worse, they sell into your buy.
* **Failed transactions.** Slippage tolerance exceeded, blockhash expired,
  congestion. Fees are still burned on a landed-but-reverted transaction.
* **Account rent.** The first purchase of any token creates an associated
  token account, which costs rent that is never recovered while held.

Every one of these makes results worse. That is the point: a strategy that
only survives without them was never viable.
"""

from dataclasses import dataclass
from enum import Enum
import math
import random

from .amm import (
    DEFAULT_POOL_FEE_BPS,
    PoolState,
    apply_trade,
    buy_output,
    pool_from_snapshot,
    sell_output,
)
from .config import Settings

LAMPORTS_PER_SOL = 1_000_000_000
BASE_FEE_LAMPORTS = 5_000
"""Solana's per-signature base fee."""

ATA_RENT_LAMPORTS = 2_039_280
"""Rent-exempt minimum for an associated token account.

This is a *deposit*, not a fee: it is locked up while the token is held and
refunded when the account is closed after a full exit. Modelling it as a
permanent cost overstates fees badly -- at a 1% position size it dwarfs the
actual transaction fees -- so the simulator refunds it on close. What it does
cost is the capital tied up while the position is open, plus one more
transaction fee to close the account."""


class FailureReason(str, Enum):
    """Why a swap did not execute."""

    SLIPPAGE_EXCEEDED = "slippage_exceeded"
    BLOCKHASH_EXPIRED = "blockhash_expired"
    CONGESTION_DROPPED = "congestion_dropped"
    NO_LIQUIDITY = "no_liquidity"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """The outcome of one attempted swap."""

    succeeded: bool
    tokens: float = 0.0
    """Tokens received (buy) or sold (sell)."""
    usd: float = 0.0
    """USD spent before costs (buy) or received before costs (sell)."""
    effective_price_usd: float = 0.0
    price_impact: float = 0.0
    latency_seconds: float = 0.0
    drift: float = 0.0
    """Fractional price move during latency; negative is adverse on a buy."""
    sandwiched: bool = False
    network_fee_usd: float = 0.0
    """Base plus priority fee. Burned whether or not the swap succeeded."""
    pool_fee_usd: float = 0.0
    rent_usd: float = 0.0
    """Rent deposited on the first buy of a token. Refundable on close."""
    rent_refund_usd: float = 0.0
    """Rent reclaimed by closing the token account after a full exit."""
    failure: FailureReason | None = None

    @property
    def total_cost_usd(self) -> float:
        """Net cash cost of this attempt, after any rent refund."""

        return (
            self.network_fee_usd + self.pool_fee_usd
            + self.rent_usd - self.rent_refund_usd
        )


class ExecutionSimulator:
    """Simulates the path from decision to confirmed fill.

    Deterministic for a given seed so that runs are reproducible and tests can
    assert on specific outcomes.
    """

    def __init__(self, settings: Settings, seed: int | None = None) -> None:
        self.settings = settings
        self.rng = random.Random(seed)
        self._owned_accounts: set[str] = set()

    # --- Public API -------------------------------------------------------

    def buy(
        self, mint: str, usd_in: float, price_usd: float, liquidity_usd: float,
        volatility: float = 0.0,
    ) -> ExecutionResult:
        """Attempt to buy ``usd_in`` worth of ``mint``."""

        pool = pool_from_snapshot(liquidity_usd, price_usd)
        if pool is None or usd_in <= 0:
            return self._failed(FailureReason.NO_LIQUIDITY, charge_fee=False)

        latency = self._latency()
        drift = self._drift(volatility, latency)
        network_fee = self._network_fee_usd()

        # A tx that never lands costs nothing; one that lands and reverts
        # still burns its fees.
        dropped = self.rng.random() < self.settings.tx_drop_rate
        if dropped:
            return self._failed(
                FailureReason.CONGESTION_DROPPED, charge_fee=False,
                latency=latency, drift=drift,
            )

        # Price moved while the transaction was in flight.
        drifted_price = price_usd * (1.0 + drift)
        if drifted_price <= 0:
            return self._failed(
                FailureReason.NO_LIQUIDITY, charge_fee=True,
                network_fee=network_fee, latency=latency, drift=drift,
            )
        pool = pool_from_snapshot(liquidity_usd, drifted_price) or pool

        # A sandwich attacker front-runs, worsening the pool we execute into.
        sandwiched = self._sandwich_hits(usd_in, liquidity_usd)
        if sandwiched:
            attack_usd = usd_in * self.settings.sandwich_size_multiple
            stolen_tokens, _ = buy_output(pool, attack_usd)
            pool = apply_trade(pool, -stolen_tokens, attack_usd)

        tokens, impact = buy_output(pool, usd_in, self._pool_fee_bps())
        if tokens <= 0:
            return self._failed(
                FailureReason.NO_LIQUIDITY, charge_fee=True,
                network_fee=network_fee, latency=latency, drift=drift,
            )

        realized_price = usd_in / tokens
        # Slippage is measured against the price the decision was made at.
        total_slip = (realized_price - price_usd) / price_usd
        if total_slip > self.settings.max_slippage_pct:
            return self._failed(
                FailureReason.SLIPPAGE_EXCEEDED, charge_fee=True,
                network_fee=network_fee, latency=latency, drift=drift,
                sandwiched=sandwiched,
            )

        rent_usd = 0.0
        if mint not in self._owned_accounts:
            self._owned_accounts.add(mint)
            rent_usd = self._lamports_to_usd(ATA_RENT_LAMPORTS)

        return ExecutionResult(
            succeeded=True,
            tokens=tokens,
            usd=usd_in,
            effective_price_usd=realized_price,
            price_impact=impact,
            latency_seconds=latency,
            drift=drift,
            sandwiched=sandwiched,
            network_fee_usd=network_fee,
            pool_fee_usd=usd_in * self._pool_fee_bps() / 10_000.0,
            rent_usd=rent_usd,
        )

    def sell(
        self, mint: str, tokens_in: float, price_usd: float,
        liquidity_usd: float, volatility: float = 0.0,
        urgent: bool = False, close_account: bool = False,
    ) -> ExecutionResult:
        """Attempt to sell ``tokens_in`` of ``mint``.

        ``urgent`` marks a stop-loss or rug exit, where a wider slippage
        tolerance is accepted because not exiting is worse than exiting badly.
        Sells are not sandwiched: an attacker profits by front-running buys.

        ``close_account`` closes the token account after a full exit, which
        refunds the rent deposit at the cost of one more transaction fee.
        """

        pool = pool_from_snapshot(liquidity_usd, price_usd)
        if pool is None or tokens_in <= 0:
            return self._failed(FailureReason.NO_LIQUIDITY, charge_fee=False)

        latency = self._latency()
        drift = self._drift(volatility, latency)
        network_fee = self._network_fee_usd()

        if self.rng.random() < self.settings.tx_drop_rate:
            return self._failed(
                FailureReason.CONGESTION_DROPPED, charge_fee=False,
                latency=latency, drift=drift,
            )

        drifted_price = price_usd * (1.0 + drift)
        if drifted_price <= 0:
            return self._failed(
                FailureReason.NO_LIQUIDITY, charge_fee=True,
                network_fee=network_fee, latency=latency, drift=drift,
            )
        pool = pool_from_snapshot(liquidity_usd, drifted_price) or pool

        usd_out, impact = sell_output(pool, tokens_in, self._pool_fee_bps())
        if usd_out <= 0:
            return self._failed(
                FailureReason.NO_LIQUIDITY, charge_fee=True,
                network_fee=network_fee, latency=latency, drift=drift,
            )

        realized_price = usd_out / tokens_in
        total_slip = (price_usd - realized_price) / price_usd
        tolerance = (
            self.settings.urgent_slippage_pct if urgent
            else self.settings.max_slippage_pct
        )
        if total_slip > tolerance:
            return self._failed(
                FailureReason.SLIPPAGE_EXCEEDED, charge_fee=True,
                network_fee=network_fee, latency=latency, drift=drift,
            )

        refund = 0.0
        if close_account and mint in self._owned_accounts:
            self._owned_accounts.discard(mint)
            refund = self._lamports_to_usd(ATA_RENT_LAMPORTS)
            # Closing the account is itself a transaction.
            network_fee += self._lamports_to_usd(BASE_FEE_LAMPORTS)

        return ExecutionResult(
            succeeded=True,
            tokens=tokens_in,
            usd=usd_out,
            effective_price_usd=realized_price,
            price_impact=impact,
            latency_seconds=latency,
            drift=drift,
            network_fee_usd=network_fee,
            pool_fee_usd=usd_out * self._pool_fee_bps() / 10_000.0,
            rent_refund_usd=refund,
        )

    # --- Internals --------------------------------------------------------

    def _failed(
        self, reason: FailureReason, *, charge_fee: bool,
        network_fee: float = 0.0, latency: float = 0.0, drift: float = 0.0,
        sandwiched: bool = False,
    ) -> ExecutionResult:
        """Build a failed result, charging fees only when the tx landed."""

        return ExecutionResult(
            succeeded=False,
            failure=reason,
            latency_seconds=latency,
            drift=drift,
            sandwiched=sandwiched,
            network_fee_usd=network_fee if charge_fee else 0.0,
        )

    def _latency(self) -> float:
        """Seconds from decision to confirmation.

        Log-normal: usually near the median, occasionally far worse, which is
        how block inclusion actually behaves under congestion.
        """

        median = self.settings.execution_latency_seconds
        if median <= 0:
            return 0.0
        return median * math.exp(self.rng.gauss(0.0, 0.5))

    def _drift(self, volatility: float, latency: float) -> float:
        """Fractional price move during ``latency`` seconds.

        Scales with the square root of time, as a random walk does. The mean
        is tilted negative because the bot buys strength: by the time an entry
        signal is visible to us it is visible to everyone, and the marginal
        buyer at that moment is usually the one who gets the worst price.
        """

        if latency <= 0:
            return 0.0
        sigma = max(0.0, volatility) * math.sqrt(latency)
        adverse_bias = -self.settings.adverse_selection_bps / 10_000.0
        return self.rng.gauss(adverse_bias, sigma)

    def _sandwich_hits(self, usd_in: float, liquidity_usd: float) -> bool:
        """Whether a sandwich bot front-runs this buy.

        Attackers need the victim's impact to exceed their own costs, so the
        risk scales with how large the order is relative to the pool.
        """

        if liquidity_usd <= 0 or self.settings.sandwich_base_rate <= 0:
            return False
        pool_fraction = usd_in / liquidity_usd
        # A trade worth 1% of the pool is a juicy target; dust is ignored.
        probability = min(
            self.settings.sandwich_base_rate * (pool_fraction / 0.01), 0.9
        )
        return self.rng.random() < probability

    def _network_fee_usd(self) -> float:
        """Base fee plus priority fee, in USD.

        Priority fees are lognormal: mostly the configured level, sometimes
        far higher when the network is busy, which is exactly when meme coins
        are moving and the bot most wants to transact.
        """

        priority = self.settings.priority_fee_lamports * math.exp(
            self.rng.gauss(0.0, self.settings.priority_fee_volatility)
        )
        return self._lamports_to_usd(BASE_FEE_LAMPORTS + priority)

    def _lamports_to_usd(self, lamports: float) -> float:
        """Convert lamports to USD at the configured SOL price."""

        return (lamports / LAMPORTS_PER_SOL) * self.settings.sol_price_usd

    def _pool_fee_bps(self) -> float:
        """The AMM's swap fee in basis points."""

        return self.settings.pool_fee_bps or DEFAULT_POOL_FEE_BPS
