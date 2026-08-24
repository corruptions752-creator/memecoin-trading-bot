"""Market data from the public DexScreener API.

Standard library only, so the bot has no install step. The client is
deliberately defensive: the feed returns partial and malformed records
routinely, and a missing field must never be read as a favourable value.
"""

from typing import Any, Iterable, Protocol
from urllib.parse import quote
import json
import logging
import time
import urllib.error
import urllib.request

from .config import Settings
from .models import TokenSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.dexscreener.com"
USER_AGENT = "memecoin-bot/0.1 (+risk-managed paper trading)"


class MarketData(Protocol):
    """Supplies candidate tokens and current prices."""

    def discover(self) -> list[TokenSnapshot]:
        """Return current candidate pairs."""

    def snapshot(self, mint: str) -> TokenSnapshot | None:
        """Return a fresh snapshot for one mint, or ``None`` if unavailable."""


class DexScreenerClient:
    """Read-only client for DexScreener's public endpoints."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._chain = "solana"

    # --- Public API -------------------------------------------------------

    def discover(self) -> list[TokenSnapshot]:
        """Gather candidate Solana pairs from several angles.

        A single search for "SOL" returns only a handful of pairs, and the
        ones it does return skew old and thin -- a live run scanned 16 and
        rejected every one on volume or age. There is no public "trending"
        endpoint on the free tier, so breadth has to come from combining
        sources:

        * the boosted-token feed, which is where actively promoted (and so
          actively traded) tokens surface;
        * searches against the common quote assets, which return the deepest
          pairs on each;
        * searches for the venues themselves, which surface recently created
          pools.

        Results are deduplicated by mint, keeping the deepest pool for each,
        since that is the one a trade would route through.
        """

        collected: dict[str, TokenSnapshot] = {}

        for snapshot in self._boosted_tokens():
            self._keep_deepest(collected, snapshot)

        for term in self._SEARCH_TERMS:
            payload = self._get(f"/latest/dex/search?q={quote(term)}")
            if payload is None:
                continue
            for snapshot in self._parse_pairs(payload.get("pairs") or []):
                self._keep_deepest(collected, snapshot)

        log.info("discovery found %d distinct token(s)", len(collected))
        return list(collected.values())

    _SEARCH_TERMS = (
        "SOL", "USDC", "SOL/USDC", "WSOL",
        "raydium", "pumpfun", "meteora", "orca",
    )
    """Deliberately broad. Each term returns a different slice, and the screen
    is strict enough that a wide net costs nothing but a few requests."""

    _MAX_BOOSTED = 60

    def _boosted_tokens(self) -> list[TokenSnapshot]:
        """Pairs for tokens on the boosted feed.

        Boosted tokens are ones someone paid to promote, which correlates
        with activity. That is a signal about attention, not about quality --
        the safety screen still has to reject the rugs among them, and it
        does.
        """

        payload = self._get_list("/token-boosts/latest/v1")
        if not payload:
            return []

        snapshots: list[TokenSnapshot] = []
        seen = 0
        for entry in payload:
            if seen >= self._MAX_BOOSTED:
                break
            if not isinstance(entry, dict):
                continue
            if entry.get("chainId") != self._chain:
                continue
            mint = str(entry.get("tokenAddress") or "").strip()
            if not mint:
                continue
            seen += 1
            snapshot = self.snapshot(mint)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    @staticmethod
    def _keep_deepest(
        collected: dict[str, TokenSnapshot], snapshot: TokenSnapshot
    ) -> None:
        """Record a snapshot, preferring the deepest pool per mint."""

        existing = collected.get(snapshot.mint)
        if existing is None or snapshot.liquidity_usd > existing.liquidity_usd:
            collected[snapshot.mint] = snapshot

    def snapshot(self, mint: str) -> TokenSnapshot | None:
        """Fetch the most liquid pair for ``mint``."""

        payload = self._get(f"/latest/dex/tokens/{quote(mint)}")
        if payload is None:
            return None
        pairs = self._parse_pairs(payload.get("pairs") or [])
        if not pairs:
            return None
        # Deepest pool is the one a trade would actually route through.
        return max(pairs, key=lambda pair: pair.liquidity_usd)

    # --- Internals --------------------------------------------------------

    def _get(self, path: str) -> dict[str, Any] | None:
        """GET a JSON object, returning ``None`` on any failure.

        A data outage must look like "no candidates", never like an exception
        that kills the trading loop while positions are open.
        """

        document = self._get_json(path)
        return document if isinstance(document, dict) else None

    def _get_json(self, path: str) -> Any:
        """GET and decode JSON, returning ``None`` on any failure."""

        url = f"{BASE_URL}{path}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.request_timeout_seconds
            ) as response:
                if response.status != 200:
                    log.warning("market data %s returned %s", path, response.status)
                    return None
                body = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            log.warning("market data request failed: %s", error)
            return None

        try:
            return json.loads(body)
        except json.JSONDecodeError:
            log.warning("market data returned invalid JSON for %s", path)
            return None

    def _get_list(self, path: str) -> list[Any]:
        """GET a JSON array endpoint, returning [] on any failure.

        Separate from :meth:`_get` because some DexScreener endpoints return
        a bare array rather than an object.
        """

        document = self._get_json(path)
        return document if isinstance(document, list) else []

    def _parse_pairs(self, raw_pairs: Iterable[Any]) -> list[TokenSnapshot]:
        """Convert raw pair records into snapshots, dropping unusable ones."""

        snapshots: list[TokenSnapshot] = []
        for raw in raw_pairs:
            snapshot = self._parse_pair(raw)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    def _parse_pair(self, raw: Any) -> TokenSnapshot | None:
        """Convert one pair record, or return ``None`` if it is unusable."""

        if not isinstance(raw, dict):
            return None
        if raw.get("chainId") != self._chain:
            return None

        base = raw.get("baseToken")
        if not isinstance(base, dict):
            return None
        mint = str(base.get("address") or "").strip()
        if not mint:
            return None

        price = _number(raw.get("priceUsd"))
        if price <= 0:
            return None

        txns_5m = _nested(raw, "txns", "m5") or {}
        created_ms = _number(raw.get("pairCreatedAt"))

        return TokenSnapshot(
            mint=mint,
            symbol=str(base.get("symbol") or "?").strip()[:32],
            pair_address=str(raw.get("pairAddress") or "").strip(),
            price_usd=price,
            liquidity_usd=_number(_nested(raw, "liquidity", "usd")),
            fdv_usd=_number(raw.get("fdv")),
            volume_24h_usd=_number(_nested(raw, "volume", "h24")),
            volume_5m_usd=_number(_nested(raw, "volume", "m5")),
            # The feed reports percentages; the model works in fractions.
            price_change_5m=_number(_nested(raw, "priceChange", "m5")) / 100.0,
            price_change_1h=_number(_nested(raw, "priceChange", "h1")) / 100.0,
            price_change_24h=_number(_nested(raw, "priceChange", "h24")) / 100.0,
            buys_5m=int(_number(txns_5m.get("buys"))),
            sells_5m=int(_number(txns_5m.get("sells"))),
            pair_created_at=created_ms / 1000.0 if created_ms > 0 else 0.0,
            fetched_at=time.time(),
        )


def _nested(source: Any, *keys: str) -> Any:
    """Walk nested dictionaries, returning ``None`` at the first miss."""

    current = source
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _number(value: Any) -> float:
    """Coerce a feed value to a float, treating anything unusable as zero."""

    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value) if _finite(float(value)) else 0.0
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return 0.0
        return parsed if _finite(parsed) else 0.0
    return 0.0


def _finite(value: float) -> bool:
    """Whether a float is usable (not NaN and not infinite)."""

    return value == value and value not in (float("inf"), float("-inf"))
