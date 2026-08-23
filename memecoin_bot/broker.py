"""Execution.

:class:`PaperBroker` routes every simulated order through
:class:`~memecoin_bot.execution.ExecutionSimulator`, so paper fills carry real
constant-product price impact, latency, adverse selection, sandwich risk,
transaction failures, and the full Solana fee stack.

:class:`LiveBroker` is the seam where real swaps would go. It refuses to run
until it is genuinely implemented, rather than silently doing nothing or,
worse, half of something.
"""

from typing import Protocol
import logging

from .config import Settings
from .execution import ExecutionResult, ExecutionSimulator
from .models import Fill, Side, TokenSnapshot

log = logging.getLogger(__name__)


class ExecutionFailed(Exception):
    """A swap did not execute. Carries the cost that was burned anyway."""

    def __init__(self, result: ExecutionResult) -> None:
        super().__init__(
            f"{result.failure.value if result.failure else 'unknown'}"
        )
        self.result = result


class Broker(Protocol):
    """Places buys and sells for a mint."""

    def buy(self, snapshot: TokenSnapshot, usd: float, reason: str = "") -> Fill:
        """Spend ``usd`` on the token and return the resulting fill."""

    def sell(
        self, snapshot: TokenSnapshot, quantity: float, reason: str = "",
        urgent: bool = False, close_account: bool = False,
    ) -> Fill:
        """Sell ``quantity`` of the token and return the resulting fill."""


class PaperBroker:
    """Simulated execution against real quoted prices.

    Costs are modelled pessimistically on purpose. A paper run that looks
    profitable only because it assumed free, instant, always-successful fills
    has taught you nothing.
    """

    def __init__(self, settings: Settings, seed: int | None = None) -> None:
        self.settings = settings
        self.simulator = ExecutionSimulator(settings, seed=seed)
        self.fills: list[Fill] = []
        self.failures: list[ExecutionResult] = []
        self.sandwich_log: list[bool] = []
        """Parallel to ``fills``: whether each fill was sandwiched."""

    def buy(self, snapshot: TokenSnapshot, usd: float, reason: str = "") -> Fill:
        """Simulate buying ``usd`` worth, raising on a failed transaction."""

        if usd <= 0:
            raise ValueError("buy size must be positive")
        if snapshot.price_usd <= 0:
            raise ValueError("cannot buy without a valid price")

        result = self.simulator.buy(
            snapshot.mint, usd, snapshot.price_usd, snapshot.liquidity_usd,
            volatility=_volatility(snapshot),
        )
        if not result.succeeded:
            self.failures.append(result)
            log.info(
                "PAPER BUY FAILED %s (%s), burned $%.4f in fees",
                snapshot.symbol,
                result.failure.value if result.failure else "unknown",
                result.network_fee_usd,
            )
            raise ExecutionFailed(result)

        fill = Fill(
            mint=snapshot.mint,
            symbol=snapshot.symbol,
            side=Side.BUY,
            price_usd=result.effective_price_usd,
            quantity=result.tokens,
            gross_usd=result.usd,
            fee_usd=result.network_fee_usd + result.pool_fee_usd + result.rent_usd,
            slippage_usd=result.usd * result.price_impact,
            at=snapshot.fetched_at,
            reason=reason,
        )
        self.fills.append(fill)
        self.sandwich_log.append(result.sandwiched)
        log.info(
            "PAPER BUY %s $%.2f @ %.10f (impact %.2f%%, drift %.2f%%, "
            "%.1fs%s)",
            snapshot.symbol, usd, result.effective_price_usd,
            result.price_impact * 100, result.drift * 100,
            result.latency_seconds, ", SANDWICHED" if result.sandwiched else "",
        )
        return fill

    def sell(
        self, snapshot: TokenSnapshot, quantity: float, reason: str = "",
        urgent: bool = False, close_account: bool = False,
    ) -> Fill:
        """Simulate selling ``quantity``, raising on a failed transaction."""

        if quantity <= 0:
            raise ValueError("sell size must be positive")

        if snapshot.price_usd <= 0:
            # A dead price feed means the position is realistically worthless.
            fill = Fill(
                mint=snapshot.mint, symbol=snapshot.symbol, side=Side.SELL,
                price_usd=0.0, quantity=quantity, gross_usd=0.0,
                fee_usd=0.0, slippage_usd=0.0, at=snapshot.fetched_at,
                reason=reason or "no price available",
            )
            self.fills.append(fill)
            self.sandwich_log.append(False)
            return fill

        result = self.simulator.sell(
            snapshot.mint, quantity, snapshot.price_usd, snapshot.liquidity_usd,
            volatility=_volatility(snapshot), urgent=urgent,
            close_account=close_account,
        )
        if not result.succeeded:
            self.failures.append(result)
            log.warning(
                "PAPER SELL FAILED %s (%s) — position still open",
                snapshot.symbol,
                result.failure.value if result.failure else "unknown",
            )
            raise ExecutionFailed(result)

        fill = Fill(
            mint=snapshot.mint,
            symbol=snapshot.symbol,
            side=Side.SELL,
            price_usd=result.effective_price_usd,
            quantity=quantity,
            gross_usd=result.usd,
            fee_usd=(
                result.network_fee_usd + result.pool_fee_usd
                - result.rent_refund_usd
            ),
            slippage_usd=result.usd * result.price_impact,
            at=snapshot.fetched_at,
            reason=reason,
        )
        self.fills.append(fill)
        self.sandwich_log.append(False)
        log.info(
            "PAPER SELL %s %.4f units -> $%.2f (impact %.2f%%, %.1fs)",
            snapshot.symbol, quantity, fill.net_usd,
            result.price_impact * 100, result.latency_seconds,
        )
        return fill


def _volatility(snapshot: TokenSnapshot) -> float:
    """Per-second volatility implied by recent price action.

    The 5-minute move is the freshest signal available; converting it to a
    per-second sigma lets the simulator scale in-flight drift by how wild the
    token actually is right now.
    """

    five_minute_move = abs(snapshot.price_change_5m)
    return five_minute_move / (300 ** 0.5) if five_minute_move > 0 else 0.001


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
        self, snapshot: TokenSnapshot, quantity: float, reason: str = "",
        urgent: bool = False, close_account: bool = False,
    ) -> Fill:
        raise NotImplementedError(_LIVE_MESSAGE)


_LIVE_MESSAGE = (
    "Live execution is not implemented. This bot ships in paper mode so the "
    "strategy can be measured before any funds are exposed. Implementing "
    "LiveBroker requires wallet key handling, Jupiter quote/swap calls, "
    "pre-flight transaction simulation, and fill reconciliation."
)


def build_broker(settings: Settings, seed: int | None = None) -> Broker:
    """Return the broker matching the configured mode."""

    from .config import LIVE

    if settings.mode == LIVE:
        return LiveBroker(settings)
    return PaperBroker(settings, seed=seed)
