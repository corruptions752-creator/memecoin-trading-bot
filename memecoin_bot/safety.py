"""Pre-trade screening.

This module answers one question: is this token disqualified outright?
It is deliberately a blocklist rather than a score. A token that fails any
single check is not bought, no matter how good the momentum looks, because
the failure modes here are not "the trade loses money" but "the position
cannot be sold at all".

What these checks can and cannot do
-----------------------------------
The market-structure checks below (liquidity, age, turnover, flow balance,
valuation) are computed from public pair data and are reliable.

The contract-level checks -- mint authority, freeze authority, LP lock, holder
concentration -- require chain state that the pair feed does not carry. They
are represented by :class:`TokenAuthority` and supplied by a holder that the
caller injects. Until a real on-chain provider is wired in, the default holder
returns ``None`` for every field and the screen treats unknown contract state
as a *failure*, not a pass. Refusing to trade what we cannot verify is the
entire point; an unchecked mint authority is how a token gets printed to zero
underneath a position.
"""

from dataclasses import dataclass
from typing import Protocol

from .config import Settings
from .models import SafetyReport, TokenSnapshot


@dataclass(frozen=True, slots=True)
class TokenAuthority:
    """Contract-level facts about a mint.

    ``None`` means "not known", which is never treated as safe.
    """

    mint_authority_revoked: bool | None = None
    freeze_authority_revoked: bool | None = None
    lp_locked_or_burned: bool | None = None
    top_holder_pct: float | None = None
    """Largest non-pool holder's share of supply, as a fraction."""
    sell_simulation_ok: bool | None = None
    """Whether a sell of the intended size simulated successfully. This is
    the only honeypot check that actually proves anything."""


class AuthorityProvider(Protocol):
    """Supplies contract-level facts for a mint."""

    def fetch(self, mint: str) -> TokenAuthority:
        """Return what is known about ``mint``."""


class UnknownAuthorityProvider:
    """Default provider: knows nothing, and says so.

    Wiring a real provider (an RPC client reading mint and pool accounts, plus
    a Jupiter quote-and-simulate round trip) is what unlocks live trading.
    """

    def fetch(self, mint: str) -> TokenAuthority:  # noqa: D102 - see Protocol
        return TokenAuthority()


# Maps a failure message to a short bucket, so the dashboard can show *why*
# a scan produced nothing instead of just showing nothing.
_CATEGORIES = (
    # Order matters: the first match wins, so more specific phrases must
    # come before generic words they might contain.
    ("supply overhang", "overhang"),
    ("liquidity", "liquidity"),
    ("24h volume", "volume"),
    ("pair only", "too new"),
    ("pair age", "age unknown"),
    ("likely abandoned", "abandoned"),
    ("FDV", "valuation"),
    ("wash trading", "wash traded"),
    ("sell pressure", "sell pressure"),
    ("no valid price", "no price"),
    ("mint authority", "mint-authority"),
    ("freeze authority", "freeze-authority"),
    ("LP not confirmed", "LP unproven"),
    ("holder distribution", "holders-unknown"),
    ("top holder", "whale"),
    ("honeypot", "unsellable"),
)


def categorize(failure: str) -> str:
    """Short bucket name for one failure message."""

    for needle, label in _CATEGORIES:
        if needle in failure:
            return label
    return "other"


MAX_TOP_HOLDER_PCT = 0.20
"""A single wallet above this share can exit into the pool and end the trade."""


def screen(
    snapshot: TokenSnapshot,
    settings: Settings,
    authority: TokenAuthority | None = None,
    *,
    require_contract_checks: bool = True,
) -> SafetyReport:
    """Judge whether ``snapshot`` is eligible to be bought at all.

    ``require_contract_checks`` exists so paper mode can run the full market
    screen while the on-chain provider is still a stub. It defaults to strict.
    """

    failures: list[str] = []
    warnings: list[str] = []

    # --- Market structure -------------------------------------------------
    if snapshot.price_usd <= 0:
        failures.append("no valid price")

    if snapshot.liquidity_usd < settings.min_liquidity_usd:
        failures.append(
            f"liquidity ${snapshot.liquidity_usd:,.0f} below floor "
            f"${settings.min_liquidity_usd:,.0f}"
        )
    elif snapshot.liquidity_usd > settings.max_liquidity_usd:
        # Not a safety problem, but outside the size band the strategy targets.
        warnings.append(
            f"liquidity ${snapshot.liquidity_usd:,.0f} above target band"
        )

    if snapshot.volume_24h_usd < settings.min_volume_24h_usd:
        failures.append(
            f"24h volume ${snapshot.volume_24h_usd:,.0f} below floor "
            f"${settings.min_volume_24h_usd:,.0f}"
        )

    age = snapshot.age_seconds
    if snapshot.pair_created_at <= 0:
        failures.append("pair age unknown")
    elif age < settings.min_pair_age_seconds:
        failures.append(
            f"pair only {age / 60:.0f}m old, minimum "
            f"{settings.min_pair_age_seconds / 60:.0f}m"
        )
    elif settings.max_pair_age_seconds and age > settings.max_pair_age_seconds:
        failures.append(
            f"pair {age / 86_400:.0f}d old, likely abandoned"
        )

    if snapshot.fdv_usd > settings.max_fdv_usd > 0:
        failures.append(
            f"FDV ${snapshot.fdv_usd:,.0f} above cap ${settings.max_fdv_usd:,.0f}"
        )

    # Supply overhang: how much token value exists per dollar of exit depth.
    # A small token on a thin pool is more dangerous than a large one on a
    # deep pool, which an absolute cap cannot express.
    if settings.max_fdv_to_liquidity > 0 and snapshot.liquidity_usd > 0:
        overhang = snapshot.fdv_usd / snapshot.liquidity_usd
        if overhang > settings.max_fdv_to_liquidity:
            failures.append(
                f"supply overhang {overhang:,.0f}x pool depth, above "
                f"{settings.max_fdv_to_liquidity:,.0f}x"
            )

    # A pool with far more volume than depth is usually wash traded.
    if snapshot.volume_to_liquidity_24h > 50:
        failures.append(
            f"turnover {snapshot.volume_to_liquidity_24h:.0f}x liquidity "
            "suggests wash trading"
        )

    if snapshot.buy_sell_ratio_5m < settings.min_buy_sell_ratio_5m:
        failures.append(
            f"sell pressure: buy/sell ratio {snapshot.buy_sell_ratio_5m:.2f}"
        )

    # --- Contract state ---------------------------------------------------
    if require_contract_checks:
        facts = authority if authority is not None else TokenAuthority()
        failures.extend(_contract_failures(facts))
    elif authority is not None:
        warnings.extend(
            f"unenforced: {reason}" for reason in _contract_failures(authority)
        )

    return SafetyReport(
        mint=snapshot.mint,
        passed=not failures,
        failures=tuple(failures),
        warnings=tuple(warnings),
    )


def _contract_failures(facts: TokenAuthority) -> list[str]:
    """List the contract-level reasons to refuse a mint.

    Unknown (``None``) is a failure everywhere in here by design.
    """

    failures: list[str] = []

    if facts.mint_authority_revoked is not True:
        failures.append(
            "mint authority not confirmed revoked (supply can be inflated)"
        )
    if facts.freeze_authority_revoked is not True:
        failures.append(
            "freeze authority not confirmed revoked (holdings can be frozen)"
        )
    if facts.lp_locked_or_burned is not True:
        failures.append("LP not confirmed locked or burned (rug risk)")

    if facts.top_holder_pct is None:
        failures.append("holder distribution unknown")
    elif facts.top_holder_pct > MAX_TOP_HOLDER_PCT:
        failures.append(
            f"top holder controls {facts.top_holder_pct:.0%} of supply"
        )

    if facts.sell_simulation_ok is not True:
        failures.append("sell simulation did not succeed (possible honeypot)")

    return failures
