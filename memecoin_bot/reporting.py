"""Performance reporting.

The numbers here are the whole point of paper mode. Read them before wiring
a wallet, and read the loss columns first.
"""

from dataclasses import dataclass
import sqlite3

from .models import Side
from .store import Store


@dataclass(frozen=True, slots=True)
class Performance:
    """Summary statistics over closed positions."""

    trades: int
    wins: int
    losses: int
    realized_usd: float
    total_fees_usd: float
    total_slippage_usd: float
    best_usd: float
    worst_usd: float
    average_win_usd: float
    average_loss_usd: float
    exit_breakdown: dict[str, int]

    @property
    def win_rate(self) -> float:
        """Share of closed trades that made money."""

        return self.wins / self.trades if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        """Gross wins divided by gross losses.

        Below 1.0 means the strategy loses money. Infinity means there is not
        enough data yet, not that the strategy is flawless.
        """

        gross_loss = abs(self.average_loss_usd * self.losses)
        gross_win = self.average_win_usd * self.wins
        if gross_loss <= 0:
            return float("inf") if gross_win > 0 else 0.0
        return gross_win / gross_loss

    def render(self) -> str:
        """A readable multi-line summary."""

        if not self.trades:
            return "No closed trades yet."

        lines = [
            f"Closed trades   : {self.trades}",
            f"Win rate        : {self.win_rate:.1%} ({self.wins}W / {self.losses}L)",
            f"Realized P&L    : ${self.realized_usd:,.2f}",
            f"Profit factor   : {self.profit_factor:.2f}",
            f"Average win     : ${self.average_win_usd:,.2f}",
            f"Average loss    : ${self.average_loss_usd:,.2f}",
            f"Best / worst    : ${self.best_usd:,.2f} / ${self.worst_usd:,.2f}",
            f"Costs paid      : ${self.total_fees_usd:,.2f} fees, "
            f"${self.total_slippage_usd:,.2f} slippage",
        ]
        if self.exit_breakdown:
            lines.append("Exits by reason :")
            for reason, count in sorted(
                self.exit_breakdown.items(), key=lambda item: -item[1]
            ):
                lines.append(f"  {reason:<20} {count}")
        return "\n".join(lines)


def summarize(store: Store) -> Performance:
    """Compute performance statistics from persisted trades."""

    closed: list[sqlite3.Row] = store.closed_positions()
    results = [row["realized_usd"] for row in closed]

    wins = [value for value in results if value > 0]
    losses = [value for value in results if value <= 0]

    fills = store.all_fills()
    fees = sum(fill.fee_usd for fill in fills)
    slippage = sum(fill.slippage_usd for fill in fills)

    breakdown: dict[str, int] = {}
    for row in closed:
        reason = row["close_reason"] or "unknown"
        breakdown[reason] = breakdown.get(reason, 0) + 1

    return Performance(
        trades=len(closed),
        wins=len(wins),
        losses=len(losses),
        realized_usd=sum(results),
        total_fees_usd=fees,
        total_slippage_usd=slippage,
        best_usd=max(results) if results else 0.0,
        worst_usd=min(results) if results else 0.0,
        average_win_usd=sum(wins) / len(wins) if wins else 0.0,
        average_loss_usd=sum(losses) / len(losses) if losses else 0.0,
        exit_breakdown=breakdown,
    )


def open_positions_table(store: Store) -> str:
    """A readable list of what is currently held."""

    positions = store.load_open_positions()
    if not positions:
        return "No open positions."

    lines = [f"{'SYMBOL':<12}{'QTY':>16}{'ENTRY':>16}{'COST':>12}"]
    for position in positions:
        lines.append(
            f"{position.symbol:<12}{position.quantity:>16.4f}"
            f"{position.entry_price_usd:>16.8f}{position.cost_usd:>12.2f}"
        )
    return "\n".join(lines)
