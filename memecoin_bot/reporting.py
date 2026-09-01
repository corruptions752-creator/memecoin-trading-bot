"""Performance reporting.

The numbers here are the whole point of paper mode. Read them before wiring
a wallet, and read the loss columns first.
"""

from dataclasses import dataclass, field
import sqlite3

from .models import Side
from .store import Store


def max_drawdown(equity_curve: list[float]) -> float:
    """Deepest peak-to-trough fall in an equity series, as a fraction.

    The single most useful number in the report. Average return says what
    happened; drawdown says whether you could have stayed in the seat while
    it happened.
    """

    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    # Equity cannot fall below zero, so neither can drawdown fall below -100%.
    return max(worst, -1.0)


def sharpe_ratio(returns: list[float]) -> float:
    """Mean return over its standard deviation.

    Unannualized on purpose: annualizing a handful of meme coin trades
    produces an impressive number that means nothing.
    """

    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    deviation = variance ** 0.5
    return mean / deviation if deviation > 0 else 0.0


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
    pnl_by_exit: dict[str, float] = field(default_factory=dict)
    by_strategy: dict[str, dict[str, float]] = field(default_factory=dict)
    """Trades, wins and P&L per playbook, so a losing thesis is visible
    instead of averaged into the others."""
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    expectancy_usd: float = 0.0
    """Average dollars per trade. Negative means the strategy loses money
    however the wins are dressed up."""
    largest_loss_streak: int = 0

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
            f"Expectancy      : ${self.expectancy_usd:,.2f} per trade",
            f"Max drawdown    : {self.max_drawdown:.2%}",
            f"Sharpe (raw)    : {self.sharpe:.2f}",
            f"Worst streak    : {self.largest_loss_streak} losses in a row",
            f"Costs paid      : ${self.total_fees_usd:,.2f} fees, "
            f"${self.total_slippage_usd:,.2f} slippage",
        ]
        if self.exit_breakdown:
            lines.append("Exits by reason :")
            for reason, count in sorted(
                self.exit_breakdown.items(), key=lambda item: -item[1]
            ):
                pnl = self.pnl_by_exit.get(reason, 0.0)
                lines.append(f"  {reason:<20}{count:>4}   ${pnl:>10,.2f}")
        if self.by_strategy:
            lines.append("By playbook     :")
            for name, stats in sorted(
                self.by_strategy.items(), key=lambda item: -item[1]["pnl"]
            ):
                trades = int(stats["trades"])
                rate = stats["wins"] / trades if trades else 0.0
                each = stats["pnl"] / trades if trades else 0.0
                lines.append(
                    f"  {name:<12}{trades:>4} trades  {rate:>5.0%} win  "
                    f"${stats['pnl']:>9,.2f}  ${each:>7,.2f}/trade"
                )
        return "\n".join(lines)


def summarize(store: Store, starting_bankroll_usd: float = 0.0) -> Performance:
    """Compute performance statistics from persisted trades.

    ``starting_bankroll_usd`` anchors the equity curve. Without it drawdown
    would be measured against a curve starting at zero, where the first
    losing trade divides by a near-zero peak and reports impossible values
    like -183%.
    """

    closed: list[sqlite3.Row] = store.closed_positions()
    results = [row["realized_usd"] for row in closed]

    wins = [value for value in results if value > 0]
    losses = [value for value in results if value <= 0]

    fills = store.all_fills()
    fees = sum(fill.fee_usd for fill in fills)
    slippage = sum(fill.slippage_usd for fill in fills)

    breakdown: dict[str, int] = {}
    pnl_by_exit: dict[str, float] = {}
    # Per-playbook results. Three theses averaged into one number say
    # nothing about which of them is worth keeping, so they are split here
    # and a losing playbook can be retired on its own record.
    by_strategy: dict[str, dict[str, float]] = {}
    for row in closed:
        reason = row["close_reason"] or "unknown"
        breakdown[reason] = breakdown.get(reason, 0) + 1
        pnl_by_exit[reason] = pnl_by_exit.get(reason, 0.0) + row["realized_usd"]

        try:
            name = row["strategy"] or "momentum"
        except (IndexError, KeyError):
            name = "momentum"
        stats = by_strategy.setdefault(name, {"trades": 0, "wins": 0, "pnl": 0.0})
        stats["trades"] += 1
        stats["pnl"] += row["realized_usd"]
        if row["realized_usd"] > 0:
            stats["wins"] += 1

    # Equity curve anchored at starting capital, in close order. Falling
    # back to the total risked keeps drawdown bounded when no bankroll is
    # supplied, rather than dividing by a peak of zero.
    anchor = starting_bankroll_usd
    if anchor <= 0:
        anchor = max(sum(abs(v) for v in results), 1.0)
    equity, running = [anchor], anchor
    for value in results:
        running += value
        equity.append(running)

    streak = worst_streak = 0
    for value in results:
        if value <= 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0

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
        pnl_by_exit=pnl_by_exit,
        by_strategy=by_strategy,
        max_drawdown=max_drawdown(equity) if results else 0.0,
        sharpe=sharpe_ratio(results),
        expectancy_usd=sum(results) / len(results) if results else 0.0,
        largest_loss_streak=worst_streak,
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
