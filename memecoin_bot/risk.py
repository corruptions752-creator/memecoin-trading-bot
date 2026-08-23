"""Bankroll accounting, position sizing, and the daily circuit breaker.

The rule this module enforces is that the strategy never decides how much to
spend. It proposes a trade; the risk manager decides the size, or refuses.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .config import Settings


class RiskStateStore(Protocol):
    """The persistence surface the risk manager needs."""

    def save_risk_state(
        self, *, cash_usd: float, open_cost_usd: float,
        realized_today_usd: float, current_day: str, halted: bool,
        halt_reason: str, at: float,
    ) -> None:
        """Persist bankroll and circuit-breaker state."""

    def load_risk_state(self) -> dict | None:
        """Return saved state, or ``None`` on a first run."""


def utc_day(at: float) -> str:
    """The UTC calendar day for an epoch timestamp, as ``YYYY-MM-DD``."""

    return datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass(slots=True)
class RiskManager:
    """Tracks capital and enforces the hard limits.

    ``cash_usd`` is uninvested capital. ``open_cost_usd`` is what is currently
    tied up in positions at cost. Bankroll is the sum, which means sizing
    shrinks after losses and grows after wins without any extra bookkeeping.
    """

    settings: Settings
    cash_usd: float
    open_cost_usd: float = 0.0
    realized_today_usd: float = 0.0
    current_day: str = ""
    halted: bool = False
    halt_reason: str = ""

    @classmethod
    def start(cls, settings: Settings, at: float) -> "RiskManager":
        """Create a manager holding the configured starting bankroll."""

        return cls(
            settings=settings,
            cash_usd=settings.starting_bankroll_usd,
            current_day=utc_day(at),
        )

    @classmethod
    def restore(
        cls, settings: Settings, at: float, store: "RiskStateStore | None"
    ) -> "RiskManager":
        """Restore saved state, or start fresh when there is none.

        A restart that re-reads the starting bankroll while also reloading
        open positions invents capital every time, and silently clears a loss
        halt. On a host that sleeps whenever it is idle, that is not an edge
        case -- it is the normal path.
        """

        if store is None:
            return cls.start(settings, at)

        saved = store.load_risk_state()
        if saved is None:
            manager = cls.start(settings, at)
            manager.persist(store, at)
            return manager

        return cls(
            settings=settings,
            cash_usd=saved["cash_usd"],
            open_cost_usd=saved["open_cost_usd"],
            realized_today_usd=saved["realized_today_usd"],
            current_day=saved["current_day"],
            halted=saved["halted"],
            halt_reason=saved["halt_reason"],
        )

    def persist(self, store: "RiskStateStore | None", at: float) -> None:
        """Write current state through to durable storage."""

        if store is None:
            return
        store.save_risk_state(
            cash_usd=self.cash_usd,
            open_cost_usd=self.open_cost_usd,
            realized_today_usd=self.realized_today_usd,
            current_day=self.current_day,
            halted=self.halted,
            halt_reason=self.halt_reason,
            at=at,
        )

    @property
    def bankroll_usd(self) -> float:
        """Cash plus capital at cost in open positions."""

        return self.cash_usd + self.open_cost_usd

    def roll_day(self, at: float) -> bool:
        """Reset the daily counters when the UTC day changes.

        Returns whether a rollover happened. A halt caused by the daily loss
        limit clears here; a halt set for any other reason does not, because
        those need a human to look at them.
        """

        day = utc_day(at)
        if day == self.current_day:
            return False
        self.current_day = day
        self.realized_today_usd = 0.0
        if self.halted and self.halt_reason.startswith("daily loss limit"):
            self.halted = False
            self.halt_reason = ""
        return True

    def halt(self, reason: str) -> None:
        """Stop new entries until explicitly resumed."""

        self.halted = True
        self.halt_reason = reason

    def resume(self) -> None:
        """Clear a halt. Intended for a deliberate human action."""

        self.halted = False
        self.halt_reason = ""

    def daily_loss_limit_usd(self) -> float:
        """The realized loss for the day that triggers the breaker."""

        return self.bankroll_usd * self.settings.daily_loss_limit_pct

    def can_open(self, open_positions: int, at: float) -> tuple[bool, str]:
        """Whether a new position may be opened, and why not if it may not."""

        self.roll_day(at)

        if self.halted:
            return False, f"trading halted: {self.halt_reason}"
        if open_positions >= self.settings.max_open_positions:
            return False, (
                f"at position limit ({self.settings.max_open_positions})"
            )

        size = self.position_size_usd()
        if size < self.settings.min_position_usd:
            return False, (
                f"position size ${size:,.2f} below minimum "
                f"${self.settings.min_position_usd:,.2f}"
            )
        if self.cash_usd < size:
            return False, (
                f"insufficient cash: ${self.cash_usd:,.2f} < ${size:,.2f}"
            )
        return True, ""

    def position_size_usd(self) -> float:
        """USD to commit to the next trade.

        A fixed fraction of the *whole* bankroll, not of free cash, so that
        holding three open positions does not quietly shrink the fourth.
        Capped at available cash.
        """

        target = self.bankroll_usd * self.settings.risk_fraction_per_trade
        return min(target, self.cash_usd)

    def record_buy(self, net_cost_usd: float) -> None:
        """Move capital from cash into an open position."""

        self.cash_usd -= net_cost_usd
        self.open_cost_usd += net_cost_usd

    def record_sell(
        self, proceeds_usd: float, cost_basis_usd: float, at: float
    ) -> None:
        """Return capital to cash and book the realized result."""

        self.roll_day(at)
        self.cash_usd += proceeds_usd
        self.open_cost_usd = max(0.0, self.open_cost_usd - cost_basis_usd)

        realized = proceeds_usd - cost_basis_usd
        self.realized_today_usd += realized

        limit = self.daily_loss_limit_usd()
        if self.realized_today_usd <= -limit and not self.halted:
            self.halt(
                f"daily loss limit hit: ${self.realized_today_usd:,.2f} "
                f"against a ${limit:,.2f} limit"
            )
