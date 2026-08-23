"""Entry scoring and exit rules.

Exits are the part that matters. There is no way to sell at the top, so the
rules here are built to guarantee three things instead:

1. A loss is capped (hard stop).
2. Principal comes off the table on the way up (first target at 2x).
3. A winner is never given all the way back (trailing stop on the runner).

Everything else -- the time stop, the liquidity collapse exit -- exists to get
out of positions that have stopped behaving like trades at all.
"""

from .config import Settings
from .models import (
    EntryDecision,
    ExitDecision,
    ExitReason,
    Position,
    TokenSnapshot,
)


def score_entry(snapshot: TokenSnapshot, settings: Settings) -> tuple[float, tuple[str, ...]]:
    """Rate a candidate from 0 to 1, with the reasoning behind the number.

    The score blends four signals that are cheap to compute and hard to fake
    all at once: short-term momentum, turnover, buy-side flow, and depth.
    A token that is only strong on one of them will not clear the threshold.
    """

    notes: list[str] = []

    momentum = snapshot.price_change_5m
    if momentum < settings.min_momentum_5m_pct:
        return 0.0, (f"5m momentum {momentum:.1%} below entry threshold",)
    if momentum > settings.max_momentum_5m_pct:
        return 0.0, (
            f"5m momentum {momentum:.1%} already vertical; refusing to chase",
        )

    # Momentum scores best in the middle of the band, not at its top edge.
    span = settings.max_momentum_5m_pct - settings.min_momentum_5m_pct
    position_in_band = (momentum - settings.min_momentum_5m_pct) / span if span > 0 else 0.0
    momentum_score = 1.0 - abs(position_in_band - 0.35) / 0.65
    momentum_score = _clamp(momentum_score)
    notes.append(f"5m momentum {momentum:.1%}")

    turnover = snapshot.volume_to_liquidity_24h
    turnover_score = _clamp(turnover / 10.0)
    notes.append(f"turnover {turnover:.1f}x")

    ratio = snapshot.buy_sell_ratio_5m
    flow_score = _clamp((ratio - 0.8) / 1.2)
    notes.append(f"buy/sell {ratio:.2f}")

    depth = snapshot.liquidity_usd
    depth_score = _clamp(
        (depth - settings.min_liquidity_usd)
        / max(1.0, settings.max_liquidity_usd - settings.min_liquidity_usd)
    ) ** 0.5
    notes.append(f"liquidity ${depth:,.0f}")

    # Trend agreement: a 5m pop against a collapsing 1h trend is a dead-cat
    # bounce more often than it is a reversal.
    if snapshot.price_change_1h < -0.30:
        notes.append("1h trend badly negative; halving score")
        trend_penalty = 0.5
    else:
        trend_penalty = 1.0

    score = (
        0.35 * momentum_score
        + 0.25 * turnover_score
        + 0.20 * flow_score
        + 0.20 * depth_score
    ) * trend_penalty

    return round(score, 4), tuple(notes)


def decide_entry(
    snapshot: TokenSnapshot, settings: Settings
) -> EntryDecision | None:
    """Return an entry instruction when the candidate clears the threshold."""

    score, notes = score_entry(snapshot, settings)
    if score < settings.min_entry_score:
        return None
    return EntryDecision(snapshot=snapshot, score=score, notes=notes)


def decide_exit(
    position: Position,
    snapshot: TokenSnapshot,
    settings: Settings,
    now: float,
) -> ExitDecision | None:
    """Decide whether to reduce or close ``position`` at the current price.

    Checks run worst-case first: the stop and the liquidity collapse are
    evaluated before any profit-taking, so a position that is simultaneously
    past its target and broken gets out entirely rather than scaling.
    """

    price = snapshot.price_usd
    if price <= 0:
        return ExitDecision(
            reason=ExitReason.LIQUIDITY_COLLAPSE,
            fraction=1.0,
            note="price feed returned no price",
        )

    position.mark(price)

    # --- Liquidity has drained: leave regardless of P&L -------------------
    if position.entry_liquidity_usd > 0:
        floor = position.entry_liquidity_usd * settings.liquidity_exit_floor_pct
        if snapshot.liquidity_usd < floor:
            return ExitDecision(
                reason=ExitReason.LIQUIDITY_COLLAPSE,
                fraction=1.0,
                note=(
                    f"liquidity ${snapshot.liquidity_usd:,.0f} fell below "
                    f"${floor:,.0f}"
                ),
            )

    # --- Hard stop --------------------------------------------------------
    stop_price = position.entry_price_usd * (1.0 - settings.stop_loss_pct)
    if price <= stop_price:
        return ExitDecision(
            reason=ExitReason.STOP_LOSS,
            fraction=1.0,
            note=f"price ${price:.8f} at or below stop ${stop_price:.8f}",
        )

    # --- Trailing stop on the runner, once principal is safe --------------
    if position.took_first_profit:
        trail_price = position.peak_price_usd * (1.0 - settings.trailing_stop_pct)
        if price <= trail_price:
            return ExitDecision(
                reason=ExitReason.TRAILING_STOP,
                fraction=1.0,
                note=(
                    f"gave back {settings.trailing_stop_pct:.0%} from peak "
                    f"${position.peak_price_usd:.8f}"
                ),
            )

    # --- First profit target ----------------------------------------------
    if not position.took_first_profit:
        target = position.entry_price_usd * settings.take_profit_multiple
        if price >= target:
            return ExitDecision(
                reason=ExitReason.TAKE_PROFIT,
                fraction=settings.take_profit_fraction,
                note=(
                    f"hit {settings.take_profit_multiple:.2g}x; taking "
                    f"{settings.take_profit_fraction:.0%} off"
                ),
            )

    # --- Time stop --------------------------------------------------------
    if position.age_seconds(now) >= settings.max_hold_seconds:
        return ExitDecision(
            reason=ExitReason.TIME_STOP,
            fraction=1.0,
            note=(
                f"held {position.age_seconds(now) / 3_600:.1f}h without "
                "reaching target"
            ),
        )

    return None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Constrain ``value`` to the inclusive range."""

    return max(low, min(high, value))
