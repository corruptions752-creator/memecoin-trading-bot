"""Per-token intelligence for the terminal display.

Everything here is derived from data the bot already collects. Nothing is
invented: a risk band is composed from the same checks the safety screen
runs, and a confidence figure is the strategy's own entry score rather than a
separate number dressed up to look like one.

Where a field has no source it is absent rather than estimated. There is, for
instance, no social-activity signal, because nothing in this bot reads social
data.
"""

from dataclasses import dataclass, asdict
from typing import Any

from .amm import pool_from_snapshot, buy_output
from .config import Settings
from .models import TokenSnapshot
from .safety import MAX_TOP_HOLDER_PCT, SafetyReport, TokenAuthority
from .strategy import score_entry


@dataclass(frozen=True, slots=True)
class RiskFactor:
    """One contributor to a token's risk band."""

    name: str
    level: str          # ok | warn | danger | unknown
    detail: str


#: Trades in the five-minute window below which flow says nothing. Two buys
#: and no sells is a 100% buy rate and means nothing at all.
MIN_FLOW_SAMPLE = 10

SIGNAL_BUY = "BUY"
SIGNAL_WATCH = "WATCH"
SIGNAL_AVOID = "AVOID"
SIGNAL_HELD = "HELD"


def assess(
    snapshot: TokenSnapshot,
    settings: Settings,
    authority: TokenAuthority | None = None,
    verdict: SafetyReport | None = None,
    position_size_usd: float = 0.0,
    held: bool = False,
) -> dict[str, Any]:
    """Everything the terminal shows about one token.

    ``authority`` and ``verdict`` are optional: a token that never reached
    on-chain verification simply reports those factors as unknown, which is
    the truth rather than a gap to be filled in.
    """

    score, notes = score_entry(snapshot, settings)
    factors = _risk_factors(snapshot, settings, authority, position_size_usd)
    band, risk_score = _risk_band(factors)

    if held:
        signal = SIGNAL_HELD
    elif verdict is not None and not verdict.passed:
        signal = SIGNAL_AVOID
    elif score >= settings.min_entry_score:
        signal = SIGNAL_BUY
    elif score > 0:
        signal = SIGNAL_WATCH
    else:
        signal = SIGNAL_AVOID

    return {
        "mint": snapshot.mint,
        "symbol": snapshot.symbol,
        "price": snapshot.price_usd,
        "change_5m": snapshot.price_change_5m,
        "change_1h": snapshot.price_change_1h,
        "change_24h": snapshot.price_change_24h,
        "liquidity": snapshot.liquidity_usd,
        "fdv": snapshot.fdv_usd,
        "volume_24h": snapshot.volume_24h_usd,
        "volume_5m": snapshot.volume_5m_usd,
        "buys_5m": snapshot.buys_5m,
        "sells_5m": snapshot.sells_5m,
        "buy_ratio": snapshot.buy_sell_ratio_5m,
        "turnover": snapshot.volume_to_liquidity_24h,
        "age_hours": snapshot.age_seconds / 3600.0,
        "confidence": score,
        "why": list(notes),
        "signal": signal,
        "risk_band": band,
        "risk_score": risk_score,
        "risk_factors": [asdict(f) for f in factors],
        "badges": _badges(snapshot, settings, score, band, signal),
        "blocked_by": list(verdict.failures) if verdict else [],
    }


def _risk_factors(
    snapshot: TokenSnapshot, settings: Settings,
    authority: TokenAuthority | None, position_size_usd: float,
) -> list[RiskFactor]:
    """The individual risks, each from a real measurement."""

    factors: list[RiskFactor] = []

    depth = snapshot.liquidity_usd
    factors.append(RiskFactor(
        "Liquidity depth",
        "ok" if depth >= settings.min_liquidity_usd * 4
        else "warn" if depth >= settings.min_liquidity_usd else "danger",
        f"${depth:,.0f} in the pool",
    ))

    if depth > 0 and snapshot.fdv_usd > 0:
        overhang = snapshot.fdv_usd / depth
        factors.append(RiskFactor(
            "Supply overhang",
            "ok" if overhang <= 100 else
            "warn" if overhang <= settings.max_fdv_to_liquidity else "danger",
            f"{overhang:,.0f}x more token value than pool depth",
        ))

    turnover = snapshot.volume_to_liquidity_24h
    factors.append(RiskFactor(
        "Volume anomaly",
        "ok" if turnover <= 20 else "warn" if turnover <= 50 else "danger",
        f"turnover {turnover:,.1f}x liquidity in 24h",
    ))

    # Slippage the bot's own position size would actually pay.
    if position_size_usd > 0:
        pool = pool_from_snapshot(depth, snapshot.price_usd)
        if pool is not None:
            _, impact = buy_output(pool, position_size_usd)
            factors.append(RiskFactor(
                "Slippage",
                "ok" if impact < 0.02 else "warn" if impact < 0.06 else "danger",
                f"{impact:.2%} on a ${position_size_usd:,.0f} order",
            ))

    a = authority
    factors.append(RiskFactor(
        "Mint authority",
        "unknown" if a is None or a.mint_authority_revoked is None
        else "ok" if a.mint_authority_revoked else "danger",
        "revoked" if a and a.mint_authority_revoked
        else "still live — supply can be inflated"
        if a and a.mint_authority_revoked is False else "not checked",
    ))
    factors.append(RiskFactor(
        "Freeze authority",
        "unknown" if a is None or a.freeze_authority_revoked is None
        else "ok" if a.freeze_authority_revoked else "danger",
        "revoked" if a and a.freeze_authority_revoked
        else "still live — holdings can be frozen"
        if a and a.freeze_authority_revoked is False else "not checked",
    ))
    factors.append(RiskFactor(
        "Holder concentration",
        "unknown" if a is None or a.top_holder_pct is None
        else "ok" if a.top_holder_pct <= MAX_TOP_HOLDER_PCT else "danger",
        f"top wallet holds {a.top_holder_pct:.1%}"
        if a and a.top_holder_pct is not None else "not checked",
    ))
    factors.append(RiskFactor(
        "Sell simulation",
        "unknown" if a is None or a.sell_simulation_ok is None
        else "ok" if a.sell_simulation_ok else "danger",
        "a sell routes cleanly" if a and a.sell_simulation_ok
        else "no sell route — possible honeypot"
        if a and a.sell_simulation_ok is False else "not checked",
    ))
    return factors


_WEIGHT = {"ok": 0, "warn": 1, "unknown": 1, "danger": 3}


def _risk_band(factors: list[RiskFactor]) -> tuple[str, int]:
    """Collapse the factors into a band and a 0-100 score.

    A single danger dominates: it takes only one unrevoked mint authority to
    lose everything, so risk here is not an average.
    """

    if not factors:
        return "UNKNOWN", 50

    worst = max(_WEIGHT[f.level] for f in factors)
    total = sum(_WEIGHT[f.level] for f in factors)
    ceiling = 3 * len(factors)
    score = int(round(100 * (1 - total / ceiling))) if ceiling else 50

    if worst >= 3:
        return "DANGER", min(score, 35)
    if total >= 3:
        return "ELEVATED", min(score, 70)
    if total >= 1:
        return "MODERATE", score
    return "LOW", score


def _badges(
    snapshot: TokenSnapshot, settings: Settings, score: float,
    band: str, signal: str,
) -> list[str]:
    """Short labels, each earned by a specific measurement."""

    badges: list[str] = []
    if band == "LOW":
        badges.append("LOW RISK")
    if band == "DANGER":
        badges.append("DANGER")
    if snapshot.price_change_5m >= 0.15:
        badges.append("HIGH MOMENTUM")
    # Only on a token the bot would actually buy. Scoring above the
    # threshold is not the same thing: a pair with a drained pool still
    # scores on momentum and flow, and a reader takes this badge to mean
    # the bot likes the token rather than that one number cleared a bar.
    if signal == SIGNAL_BUY and score >= settings.min_entry_score:
        badges.append("AI SIGNAL")
    flow = snapshot.buys_5m + snapshot.sells_5m
    if flow >= MIN_FLOW_SAMPLE and snapshot.buy_sell_ratio_5m >= 2.0:
        badges.append("BUY PRESSURE")
    if snapshot.volume_to_liquidity_24h >= 30:
        badges.append("HIGH VOLUME")
    if signal == SIGNAL_WATCH:
        badges.append("WATCH")
    if snapshot.age_seconds < 3 * 3600:
        badges.append("NEW PAIR")
    return badges
