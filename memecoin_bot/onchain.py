"""The real :class:`~memecoin_bot.safety.AuthorityProvider`.

Combines a Solana RPC read of the mint account with a Jupiter sell quote to
answer the contract-level questions the safety screen asks.

On the LP lock check
--------------------
Four of the five checks are answered properly here. ``lp_locked_or_burned`` is
not, and it is left as ``None`` rather than guessed, because proving it means
identifying the specific pool for the pair, finding its LP mint, and showing
that supply is burned or held by a known locker contract -- which differs per
DEX (Raydium, Orca, Meteora) and cannot be done honestly from pair data alone.

Reporting a guess as a verified fact would defeat the purpose of the screen,
so instead the policy is explicit and configurable:

* ``strict`` (default) -- unknown LP state blocks the trade. Correct, and will
  reject most tokens until a pool-aware provider exists.
* ``substitute`` -- accept depth and age as a weaker proxy, since a pool that
  has held real liquidity for hours has not been pulled *yet*. This is a real
  loosening of safety, and it is named so nobody enables it by accident.

Caching
-------
Mint state is immutable in the ways that matter here: a revoked authority
cannot be un-revoked. Results are cached so a scan of many candidates does not
issue the same RPC call repeatedly and get rate limited into failing closed.
"""

from dataclasses import dataclass
import logging
import time

from .chain import SolanaRpcClient
from .config import LP_SUBSTITUTE, Settings, resolve_lp_policy
from .jupiter import JupiterClient
from .safety import TokenAuthority

log = logging.getLogger(__name__)

LP_POLICY_STRICT = "strict"
LP_POLICY_SUBSTITUTE = "substitute"


@dataclass(slots=True)
class _CacheEntry:
    """A cached lookup and when it was taken."""

    authority: TokenAuthority
    at: float


class OnChainAuthorityProvider:
    """Answers contract-level questions from chain state and quotes."""

    def __init__(
        self,
        settings: Settings,
        rpc: SolanaRpcClient | None = None,
        jupiter: JupiterClient | None = None,
    ) -> None:
        self.settings = settings
        self.rpc = rpc or SolanaRpcClient(
            settings.rpc_endpoint, settings.request_timeout_seconds
        )
        self.jupiter = jupiter or JupiterClient(
            settings.jupiter_endpoint, settings.request_timeout_seconds
        )
        self._cache: dict[str, _CacheEntry] = {}
        self.lookups = 0
        self.rpc_failures = 0
        """A lookup that could not read the mint at all. Distinguishes 'this
        token is unsafe' from 'the endpoint would not answer' -- which look
        identical in the verdict but mean opposite things."""

    def fetch(self, mint: str) -> TokenAuthority:
        """Return what can be verified about ``mint``.

        Never raises. Any failure yields ``None`` fields, which the screen
        reads as unknown and therefore rejects.
        """

        cached = self._cache.get(mint)
        now = time.time()
        if cached is not None and now - cached.at < self.settings.authority_cache_seconds:
            return cached.authority

        authority = self._fetch_uncached(mint)
        self._cache[mint] = _CacheEntry(authority=authority, at=now)
        return authority

    def _fetch_uncached(self, mint: str) -> TokenAuthority:
        """Do the actual lookups for one mint."""

        self.lookups += 1
        state = self.rpc.get_mint_state(mint)
        if state is None:
            # Without the mint account nothing else is worth asking. This is
            # also the signature of an unreachable or throttled endpoint, so
            # it is counted rather than silently folded into "unsafe".
            self.rpc_failures += 1
            return TokenAuthority()

        top_holder = self.rpc.get_top_holder_pct(mint, state.supply)

        sell_ok = None
        if state.supply > 0:
            # Simulate selling a position the size this bot would actually
            # take. Quoting a dust amount can succeed where a real position
            # would not, which would make the check worthless.
            probe = self._probe_amount(state.decimals)
            sell_ok = self.jupiter.can_sell(
                mint, probe,
                max_price_impact_pct=self.settings.max_sell_price_impact_pct,
            )

        return TokenAuthority(
            mint_authority_revoked=state.mint_authority_revoked,
            freeze_authority_revoked=state.freeze_authority_revoked,
            lp_locked_or_burned=None,   # see the module docstring
            top_holder_pct=top_holder,
            sell_simulation_ok=sell_ok,
        )

    def _probe_amount(self, decimals: int) -> int:
        """Base units to quote a sell for.

        Uses the configured probe size in whole tokens. This is a blunt
        instrument -- the honest version sizes the probe from the position the
        risk manager would actually open -- but it is enough to separate "no
        route at all" from "routes fine".
        """

        return max(1, int(self.settings.sell_probe_tokens * (10 ** decimals)))

    @property
    def rpc_failure_rate(self) -> float:
        """Share of lookups that could not read chain state at all."""

        return self.rpc_failures / self.lookups if self.lookups else 0.0

    def clear_cache(self) -> None:
        """Drop cached lookups."""

        self._cache.clear()


def apply_lp_policy(
    authority: TokenAuthority, settings: Settings, *,
    liquidity_usd: float, age_seconds: float,
) -> TokenAuthority:
    """Resolve the unknown LP lock according to the configured policy.

    Under ``substitute`` this returns an authority whose ``lp_locked_or_burned``
    is ``True`` when the pool is deep enough and old enough. That is a weaker
    claim than a real lock proof and is documented as such; under ``strict``
    the field is left unknown and the trade is refused.
    """

    if resolve_lp_policy(settings) != LP_SUBSTITUTE:
        return authority
    if authority.lp_locked_or_burned is not None:
        return authority

    deep_enough = liquidity_usd >= settings.lp_substitute_min_liquidity_usd
    old_enough = age_seconds >= settings.lp_substitute_min_age_seconds
    if deep_enough and old_enough:
        return TokenAuthority(
            mint_authority_revoked=authority.mint_authority_revoked,
            freeze_authority_revoked=authority.freeze_authority_revoked,
            lp_locked_or_burned=True,
            top_holder_pct=authority.top_holder_pct,
            sell_simulation_ok=authority.sell_simulation_ok,
        )
    return authority
