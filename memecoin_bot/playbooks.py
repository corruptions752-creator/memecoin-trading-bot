"""Independent entry strategies, each scored and accounted for separately.

Run 1 traded one thesis -- buy 5m momentum -- 107 times and lost $757. The
problem with a single thesis is not only that it can be wrong; it is that
when it is wrong there is nothing else running, and nothing in the record
tells you whether the idea or its parameters were at fault.

So entries come from named playbooks. Every position records the playbook
that opened it, which makes P&L splittable per thesis and lets a loser be
retired on evidence. Each playbook caps how many of the open slots it may
hold, so no single idea can take the whole book.

Adding an idea means adding a playbook here, not widening an existing one
until it stops meaning anything.
"""

from collections.abc import Callable
from dataclasses import dataclass

from .config import Settings
from .models import TokenSnapshot


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


Scorer = Callable[[TokenSnapshot, Settings], tuple[float, tuple[str, ...]]]


@dataclass(frozen=True)
class Playbook:
    """One entry thesis: how it scores a token and how much room it gets."""

    name: str
    thesis: str
    score: Scorer
    min_score: float
    max_share: float
    """Largest fraction of the open slots this playbook may hold at once.

    Capped rather than unlimited so one thesis firing constantly cannot
    crowd out the others and turn the book back into a single strategy.
    """

    def slot_cap(self, max_open_positions: int) -> int:
        return max(1, int(max_open_positions * self.max_share))


# --- momentum -------------------------------------------------------------

def score_momentum(
    snapshot: TokenSnapshot, settings: Settings
) -> tuple[float, tuple[str, ...]]:
    """Buy a token moving up now, on real turnover and buy-side flow.

    The original thesis, kept because the fat tail came through it: both 3x
    winners in run 1 were momentum entries.
    """

    notes: list[str] = []
    momentum = snapshot.price_change_5m
    if momentum < settings.min_momentum_5m_pct:
        return 0.0, (f"5m momentum {momentum:.1%} below entry threshold",)
    if momentum > settings.max_momentum_5m_pct:
        return 0.0, (f"5m momentum {momentum:.1%} already vertical; refusing to chase",)

    span = settings.max_momentum_5m_pct - settings.min_momentum_5m_pct
    where = (momentum - settings.min_momentum_5m_pct) / span if span > 0 else 0.0
    momentum_score = _clamp(1.0 - abs(where - 0.35) / 0.65)
    notes.append(f"5m momentum {momentum:.1%}")

    turnover = snapshot.volume_to_liquidity_24h
    notes.append(f"turnover {turnover:.1f}x")
    ratio = snapshot.buy_sell_ratio_5m
    notes.append(f"buy/sell {ratio:.2f}")
    depth = snapshot.liquidity_usd
    notes.append(f"liquidity ${depth:,.0f}")

    depth_score = _clamp(
        (depth - settings.min_liquidity_usd)
        / max(1.0, settings.max_liquidity_usd - settings.min_liquidity_usd)
    ) ** 0.5

    if snapshot.price_change_1h < -0.30:
        notes.append("1h trend badly negative; halving score")
        penalty = 0.5
    else:
        penalty = 1.0

    score = (
        0.35 * momentum_score
        + 0.25 * _clamp(turnover / 10.0)
        + 0.20 * _clamp((ratio - 0.8) / 1.2)
        + 0.20 * depth_score
    ) * penalty
    return round(score, 4), tuple(notes)


# --- reversal -------------------------------------------------------------

def score_reversal(
    snapshot: TokenSnapshot, settings: Settings
) -> tuple[float, tuple[str, ...]]:
    """Buy a sharp dip that buyers are already stepping into.

    Deliberately anti-correlated with momentum: it wants 5m NEGATIVE. The
    point of a second thesis is that it can be right in the conditions that
    make the first one wrong, so it must not fire on the same tokens.

    The dip has to be bought, not merely deep -- buy/sell flow above 1.0 on
    a red candle is someone accumulating. A dip with sellers still in
    control is just a falling knife, and the 1h floor keeps this off tokens
    that are in an outright collapse.
    """

    notes: list[str] = []
    momentum = snapshot.price_change_5m
    if momentum > -0.05:
        return 0.0, (f"5m {momentum:.1%} is not a dip",)
    if momentum < -0.45:
        return 0.0, (f"5m {momentum:.1%} is a collapse, not a dip",)

    ratio = snapshot.buy_sell_ratio_5m
    if ratio < 1.0:
        return 0.0, (f"buy/sell {ratio:.2f}: sellers still in control",)
    if snapshot.price_change_1h < -0.35:
        return 0.0, (f"1h {snapshot.price_change_1h:.1%}: falling knife",)

    # Deeper dips score higher, but only up to the collapse boundary.
    depth_of_dip = _clamp((-momentum - 0.05) / 0.40)
    notes.append(f"5m dip {momentum:.1%}")
    notes.append(f"buy/sell {ratio:.2f} into the dip")

    flow_score = _clamp((ratio - 1.0) / 1.5)
    turnover = snapshot.volume_to_liquidity_24h
    notes.append(f"turnover {turnover:.1f}x")
    depth = snapshot.liquidity_usd
    notes.append(f"liquidity ${depth:,.0f}")
    depth_score = _clamp(
        (depth - settings.min_liquidity_usd)
        / max(1.0, settings.max_liquidity_usd - settings.min_liquidity_usd)
    ) ** 0.5

    # A dip inside an intact uptrend is worth more than one in a downtrend.
    trend_bonus = 1.15 if snapshot.price_change_1h > 0 else 1.0
    if trend_bonus > 1.0:
        notes.append("dip inside an intact 1h uptrend")

    score = (
        0.35 * depth_of_dip
        + 0.30 * flow_score
        + 0.20 * _clamp(turnover / 10.0)
        + 0.15 * depth_score
    ) * trend_bonus
    return round(min(score, 1.0), 4), tuple(notes)


# --- trend ----------------------------------------------------------------

def score_trend(
    snapshot: TokenSnapshot, settings: Settings
) -> tuple[float, tuple[str, ...]]:
    """Buy an established multi-hour advance on a quiet 5m candle.

    Momentum buys the pop and pays up for it; this waits for the pause in a
    token that has already been trending for hours. The 5m band is
    deliberately narrow and near flat, so it never competes with momentum
    for the same candle.
    """

    notes: list[str] = []
    hour = snapshot.price_change_1h
    if hour < 0.20:
        return 0.0, (f"1h {hour:.1%}: no established trend",)
    if not (-0.04 <= snapshot.price_change_5m <= 0.06):
        return 0.0, (f"5m {snapshot.price_change_5m:.1%} outside the quiet band",)

    day = snapshot.price_change_24h
    if day < 0:
        return 0.0, (f"24h {day:.1%} negative; the trend is not established",)

    notes.append(f"1h trend {hour:.1%}")
    notes.append(f"24h {day:.1%}")
    notes.append(f"5m quiet at {snapshot.price_change_5m:.1%}")

    trend_score = _clamp((hour - 0.20) / 0.80)
    day_score = _clamp(day / 2.0)
    turnover = snapshot.volume_to_liquidity_24h
    notes.append(f"turnover {turnover:.1f}x")
    depth = snapshot.liquidity_usd
    notes.append(f"liquidity ${depth:,.0f}")
    depth_score = _clamp(
        (depth - settings.min_liquidity_usd)
        / max(1.0, settings.max_liquidity_usd - settings.min_liquidity_usd)
    ) ** 0.5

    score = (
        0.35 * trend_score
        + 0.25 * day_score
        + 0.20 * _clamp(turnover / 10.0)
        + 0.20 * depth_score
    )
    return round(score, 4), tuple(notes)


PLAYBOOKS: tuple[Playbook, ...] = (
    # Shares set from the 60-seed attribution run, not from taste:
    #
    #   playbook   trades  win%      P&L   per trade   mean/seed   w/o best
    #   trend         768   33%  +4093.28      +5.33      +68.22     +62.28
    #   momentum     1945   23%  -4472.93      -2.30      -74.55     -85.08
    #
    # trend is the first positive-expectancy thesis measured here and it
    # barely moves when its luckiest seed is dropped. momentum is the thesis
    # that lost $757 over 107 real trades and the sweep agrees with the
    # ledger. So trend gets the largest share and momentum is demoted --
    # not retired, because both real 3x winners came through it and the fat
    # tail is where all the profit lives.
    Playbook(
        name="trend",
        thesis="buy an established multi-hour advance on a quiet 5m candle",
        score=score_trend,
        min_score=0.0,      # falls back to settings.min_entry_score
        max_share=0.5,
    ),
    Playbook(
        name="momentum",
        thesis="buy a token moving up now, on real turnover and buy flow",
        score=score_momentum,
        min_score=0.0,
        max_share=0.375,
    ),
    # Never fired once in 60 seeds: the synthetic paths do not produce dips
    # with buy-side flow behind them. That is not evidence it is broken, and
    # not evidence it works either -- it is untested, and it gets the
    # smallest share until the live feed has ruled on it.
    Playbook(
        name="reversal",
        thesis="buy a sharp dip that buyers are already stepping into",
        score=score_reversal,
        min_score=0.0,
        max_share=0.25,
    ),
)


def by_name(name: str) -> Playbook | None:
    for playbook in PLAYBOOKS:
        if playbook.name == name:
            return playbook
    return None
