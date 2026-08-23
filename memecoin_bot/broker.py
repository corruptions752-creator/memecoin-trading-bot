"""Execution.

:class:`PaperBroker` simulates fills against live prices, charging fees and
slippage so that paper results are not flattered. :class:`LiveBroker` is the
seam where real swaps would go; it refuses to run until it is genuinely
implemented, rather than silently doing nothing or, worse, half of something.
"""

from typing import Protocol
import logging

from .config import Settings
from .models import Fill, Side, TokenSnapshot

log = logging.getLogger(__name__)


class Broker(Protocol):
    """Places buys and sells for a mint."""

    def buy(self, snapshot: TokenSnapshot, usd: float, reason: str = "") -> Fill:
        """Spend ``usd`` on the token and return the resulting fill."""

    def sell(
        self, snapshot: TokenSnapshot, quantity: float, reason: str = ""
    ) -> Fill:
        """Sell ``quantity`` of the token and return the resulting fill."""


class PaperBroker:
    """Simulated execution against real quoted prices.

    Costs are modelled pessimistically on purpose. A paper run that only looks
    profitable because it assumed free, instant fills has taught you nothing.
    Both fees and slippage are charged against the trader on each leg, and the
    priority fee is applied per transaction.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.fills: list[Fill] = []

    def buy(self, snapshot: TokenSnapshot, usd: float, reason: str = "") -> Fill:
        """Simulate buying ``usd`` worth at a slippage-adjusted price."""

        if usd <= 0:
            raise ValueError("buy size must be positive")
        if snapshot.price_usd <= 0:
            raise ValueError("cannot buy without a valid price")

        slippage_bps = self._slippage_bps(usd, snapshot)
        # A buyer pays *up*: the effective price is worse than quoted.
        effective_price = snapshot.price_usd * (1.0 + slippage_bps / 10_000.0)
        quantity = usd / effective_price

        fee_usd = usd * self.settings.assumed_fee_bps / 10_000.0
        fee_usd += self.settings.priority_fee_usd
        slippage_usd = usd * slippage_bps / 10_000.0

        fill = Fill(
            mint=snapshot.mint,
            symbol=snapshot.symbol,
            side=Side.BUY,
            price_usd=effective_price,
            quantity=quantity,
            gross_usd=usd,
            fee_usd=fee_usd,
            slippage_usd=slippage_usd,
            at=snapshot.fetched_at,
            reason=reason,
        )
        self.fills.append(fill)
        log.info(
            "PAPER BUY %s $%.2f @ %.10f (slippage %.0fbps) %s",
            snapshot.symbol, usd, effective_price, slippage_bps, reason,
        )
        return fill

    def sell(
        self, snapshot: TokenSnapshot, quantity: float, reason: str = ""
    ) -> Fill:
        """Simulate selling ``quantity`` at a slippage-adjusted price."""

        if quantity <= 0:
            raise ValueError("sell size must be positive")
        if snapshot.price_usd <= 0:
            # A dead price feed means the position is realistically worthless.
            fill = Fill(
                mint=snapshot.mint,
                symbol=snapshot.symbol,
                side=Side.SELL,
                price_usd=0.0,
                quantity=quantity,
                gross_usd=0.0,
                fee_usd=0.0,
                slippage_usd=0.0,
                at=snapshot.fetched_at,
                reason=reason or "no price available",
            )
            self.fills.append(fill)
            return fill

        notional = quantity * snapshot.price_usd
        slippage_bps = self._slippage_bps(notional, snapshot)
        # A seller receives *less*: the effective price is below quoted.
        effective_price = snapshot.price_usd * (1.0 - slippage_bps / 10_000.0)
        gross_usd = quantity * effective_price

        fee_usd = gross_usd * self.settings.assumed_fee_bps / 10_000.0
        fee_usd += self.settings.priority_fee_usd
        slippage_usd = notional * slippage_bps / 10_000.0

        fill = Fill(
            mint=snapshot.mint,
            symbol=snapshot.symbol,
            side=Side.SELL,
            price_usd=effective_price,
            quantity=quantity,
            gross_usd=gross_usd,
            fee_usd=fee_usd,
            slippage_usd=slippage_usd,
            at=snapshot.fetched_at,
            reason=reason,
        )
        self.fills.append(fill)
        log.info(
            "PAPER SELL %s %.4f units -> $%.2f (%s)",
            snapshot.symbol, quantity, gross_usd - fee_usd, reason,
        )
        return fill

    def _slippage_bps(self, usd: float, snapshot: TokenSnapshot) -> float:
        """Estimate slippage, growing with trade size relative to the pool.

        A constant-product pool moves roughly in proportion to the fraction of
        its reserves being traded, so size/liquidity drives the estimate on
        top of the configured baseline.
        """

        base = self.settings.assumed_slippage_bps
        if snapshot.liquidity_usd <= 0:
            return base * 4
        pool_fraction = usd / snapshot.liquidity_usd
        # 10,000 bps == 100%; a trade of 1% of the pool costs roughly 100bps
        # of impact on top of the baseline spread.
        impact_bps = pool_fraction * 10_000.0
        return min(base + impact_bps, 5_000.0)


class LiveBroker:
    """Real Jupiter swaps. Not implemented, and fails loudly rather than quietly.

    Finishing this class means, at minimum: loading a keypair from a secret
    (never a file in the repo), fetching a Jupiter quote with an explicit
    ``slippageBps``, simulating the transaction before signing, signing and
    sending with a priority fee, confirming the signature, and reconciling the
    actual filled amount against what was requested. Every one of those steps
    is a place where real funds are lost when it is skipped, which is why
    there is no partial version of it here.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def buy(self, snapshot: TokenSnapshot, usd: float, reason: str = "") -> Fill:
        raise NotImplementedError(_LIVE_MESSAGE)

    def sell(
        self, snapshot: TokenSnapshot, quantity: float, reason: str = ""
    ) -> Fill:
        raise NotImplementedError(_LIVE_MESSAGE)


_LIVE_MESSAGE = (
    "Live execution is not implemented. This bot ships in paper mode so the "
    "strategy can be measured before any funds are exposed. Implementing "
    "LiveBroker requires wallet key handling, Jupiter quote/swap calls, "
    "pre-flight transaction simulation, and fill reconciliation."
)


def build_broker(settings: Settings) -> Broker:
    """Return the broker matching the configured mode."""

    from .config import LIVE

    if settings.mode == LIVE:
        return LiveBroker(settings)
    return PaperBroker(settings)
