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
    snapshot: TokenSnapshot,
    settings: Settings,
    *,
    allowed: "set[str] | None" = None,
) -> EntryDecision | None:
    """Return the best entry any enabled playbook offers for this token.

    Every playbook scores the token and the highest wins, so a token that
    only one thesis likes is still tradeable while one that several like
    goes in on its strongest reading. ``allowed`` lets the caller withhold
    playbooks that are already at their slot cap.
    """

    from .playbooks import PLAYBOOKS

    best: EntryDecision | None = None
    for playbook in PLAYBOOKS:
        if allowed is not None and playbook.name not in allowed:
            continue
        score, notes = playbook.score(snapshot, settings)
        threshold = playbook.min_score or settings.min_entry_score
        if score < threshold:
            continue
        if best is None or score > best.score:
            best = EntryDecision(
                snapshot=snapshot, score=score, notes=notes,
                strategy=playbook.name,
            )
    return best


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

    # A tick that prices the position above the whole pool cannot be real:
    # there is nothing to sell into at that price. The pair feed does return
    # these -- it briefly quoted a mint at 157,000x its actual price, which
    # cleared the profit target every cycle and sent the engine into a sell
    # that could never fill. Ignore the tick rather than act on it, and do
    # not let it set the high-water mark, which would poison the trailing
    # stop permanently.
    if implausible_mark(position, snapshot):
        return None

    # A peak recorded before the guard existed, or from a tick that arrived
    # while the pool was deeper, stays poisoned forever: the high-water mark
    # only ratchets up. Repair it here rather than requiring surgery on the
    # stored position -- an unreachable trailing stop is a position that can
    # never take profit again.
    if peak_is_unreachable(position, snapshot):
        position.peak_price_usd = max(position.entry_price_usd, price)

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

    # --- Give-back floor: a winner is not allowed to become a loser -------
    # Until the first profit target trips there was nothing between entry and
    # the hard stop, so a position could run to 2.3x and still be walked all
    # the way down to -35%. The floor ratchets up with the peak and never
    # sells into strength, so a runner still runs; it only cuts a position
    # that already gave the gain back.
    if not position.took_first_profit and position.entry_price_usd > 0:
        peak_multiple = position.peak_price_usd / position.entry_price_usd
        floor_multiple = 0.0
        for trigger, floor in settings.give_back_ladder:
            if peak_multiple < trigger:
                break
            floor_multiple = floor
        if floor_multiple and price <= position.entry_price_usd * floor_multiple:
            return ExitDecision(
                reason=ExitReason.GIVE_BACK,
                fraction=1.0,
                note=(
                    f"peaked at {peak_multiple:.2f}x then fell to "
                    f"{price / position.entry_price_usd:.2f}x, through the "
                    f"{floor_multiple:.2f}x floor"
                ),
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


def implausible_mark(position: Position, snapshot: TokenSnapshot) -> bool:
    """Whether this tick prices the position above the pool behind it.

    Grounded in what an AMM can actually pay rather than in a tuned
    multiplier: a holding may legitimately be a large share of its pool, but
    never a multiple of it. A real position that grows past its pool is
    already an exit the liquidity rules handle.
    """

    if snapshot.liquidity_usd <= 0:
        return False  # nothing to compare against; other rules cover it
    return position.value_usd(snapshot.price_usd) > snapshot.liquidity_usd


def peak_is_unreachable(position: Position, snapshot: TokenSnapshot) -> bool:
    """Whether the recorded peak prices the holding above its own pool."""

    if snapshot.liquidity_usd <= 0 or position.peak_price_usd <= 0:
        return False
    return position.value_usd(position.peak_price_usd) > snapshot.liquidity_usd
