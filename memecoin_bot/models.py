"""Value types shared across the bot.

All prices are USD per token, all sizes are USD notional unless a field name
says otherwise, and all timestamps are UTC epoch seconds.
"""

from dataclasses import dataclass, field, replace
from enum import Enum
import time


class Side(str, Enum):
    """Direction of a fill."""

    BUY = "buy"
    SELL = "sell"


class ExitReason(str, Enum):
    """Why a position was reduced or closed.

    Kept as an enum so reporting can group outcomes without string matching.
    """

    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    TIME_STOP = "time_stop"
    LIQUIDITY_COLLAPSE = "liquidity_collapse"
    MANUAL = "manual"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class TokenSnapshot:
    """A point-in-time view of one tradable pair."""

    mint: str
    symbol: str
    pair_address: str
    price_usd: float
    liquidity_usd: float
    fdv_usd: float
    volume_24h_usd: float
    pool_base_amount: float = 0.0
    """Base tokens held by the pool, in whole tokens.

    The pool is normally the largest holder of a meme coin by a wide margin,
    so concentration cannot be judged without excluding it first."""
    volume_5m_usd: float = 0.0
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0
    price_change_24h: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    pair_created_at: float = 0.0
    fetched_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        """Seconds since the pair was created."""

        if self.pair_created_at <= 0:
            return 0.0
        return max(0.0, self.fetched_at - self.pair_created_at)

    @property
    def buy_sell_ratio_5m(self) -> float:
        """Buy count over sell count in the last five minutes.

        Returns a neutral 1.0 when there is no trade flow to judge, so that a
        quiet pair is filtered by the volume rules rather than this one.
        """

        if self.sells_5m <= 0:
            return 1.0 if self.buys_5m <= 0 else 2.0
        return self.buys_5m / self.sells_5m

    @property
    def volume_to_liquidity_24h(self) -> float:
        """Turnover ratio; a rough proxy for how tradable the pair really is."""

        if self.liquidity_usd <= 0:
            return 0.0
        return self.volume_24h_usd / self.liquidity_usd


@dataclass(frozen=True, slots=True)
class SafetyReport:
    """The outcome of the pre-trade screen."""

    mint: str
    passed: bool
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def describe(self) -> str:
        """Human-readable one-liner for logs and Discord messages."""

        if self.passed:
            if self.warnings:
                return f"{self.mint}: passed with warnings: " + "; ".join(self.warnings)
            return f"{self.mint}: passed"
        return f"{self.mint}: rejected: " + "; ".join(self.failures)


@dataclass(frozen=True, slots=True)
class Fill:
    """A single executed (or simulated) trade leg."""

    mint: str
    symbol: str
    side: Side
    price_usd: float
    quantity: float
    gross_usd: float
    fee_usd: float
    slippage_usd: float
    at: float
    reason: str = ""

    @property
    def net_usd(self) -> float:
        """Cash actually leaving (buy) or arriving (sell), after costs."""

        if self.side is Side.BUY:
            return self.gross_usd + self.fee_usd + self.slippage_usd
        return self.gross_usd - self.fee_usd - self.slippage_usd


@dataclass
class Position:
    """An open holding and everything the exit rules need to judge it."""

    mint: str
    symbol: str
    entry_price_usd: float
    quantity: float
    cost_usd: float
    opened_at: float
    entry_liquidity_usd: float
    peak_price_usd: float = 0.0
    realized_usd: float = 0.0
    took_first_profit: bool = False
    initial_quantity: float = 0.0
    unverified_reasons: tuple[str, ...] = ()
    """Verification findings this position was opened in spite of."""
    position_id: int | None = None

    def __post_init__(self) -> None:
        if self.peak_price_usd <= 0:
            self.peak_price_usd = self.entry_price_usd
        if self.initial_quantity <= 0:
            self.initial_quantity = self.quantity

    def mark(self, price_usd: float) -> None:
        """Record a new price, tracking the high-water mark for trailing."""

        if price_usd > self.peak_price_usd:
            self.peak_price_usd = price_usd

    def unrealized_usd(self, price_usd: float) -> float:
        """Paper gain or loss on the remaining quantity."""

        return (price_usd - self.entry_price_usd) * self.quantity

    def value_usd(self, price_usd: float) -> float:
        """Mark-to-market value of what is still held."""

        return price_usd * self.quantity

    def multiple(self, price_usd: float) -> float:
        """Current price as a multiple of the entry price."""

        if self.entry_price_usd <= 0:
            return 0.0
        return price_usd / self.entry_price_usd

    def age_seconds(self, now: float | None = None) -> float:
        """How long the position has been open."""

        return max(0.0, (time.time() if now is None else now) - self.opened_at)

    def reduced(self, quantity: float, proceeds_usd: float) -> "Position":
        """Return a copy with ``quantity`` sold for ``proceeds_usd``."""

        sold = min(quantity, self.quantity)
        return replace(
            self,
            quantity=self.quantity - sold,
            realized_usd=self.realized_usd + proceeds_usd
            - (self.entry_price_usd * sold),
        )


@dataclass(frozen=True, slots=True)
class ExitDecision:
    """An instruction from the strategy to reduce or close a position."""

    reason: ExitReason
    fraction: float
    """Fraction of the *remaining* quantity to sell, in (0, 1]."""

    note: str = ""

    @property
    def is_full_exit(self) -> bool:
        """Whether this decision closes the position outright."""

        return self.fraction >= 1.0


@dataclass(frozen=True, slots=True)
class EntryDecision:
    """An instruction from the strategy to open a position."""

    snapshot: TokenSnapshot
    score: float
    notes: tuple[str, ...] = ()
    unverified_reasons: tuple[str, ...] = ()
    """Verification findings this trade was taken in spite of. Empty when the
    token passed cleanly. Carried onto the position so results can be split
    between verified and unverified rather than averaged together."""
