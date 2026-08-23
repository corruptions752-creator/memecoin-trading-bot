"""Live connectivity and honesty check.

Everything the bot reports is derived from three external sources. This
command hits all three for real and prints the **raw response next to the
value the bot derived from it**, so the parsing can be checked rather than
trusted.

Run it whenever a number looks wrong, or before believing any claim about
what the bot can see.
"""

from dataclasses import dataclass
import json
import time
import urllib.error
import urllib.request

from .chain import SolanaRpcClient
from .config import Settings
from .jupiter import WRAPPED_SOL, JupiterClient
from .market import DexScreenerClient
from .onchain import OnChainAuthorityProvider
from .safety import screen
from .strategy import score_entry

# BONK: a large, long-lived Solana meme coin. Used as the probe token because
# it is liquid enough that every endpoint should have data for it, so a
# failure here is a real failure rather than an obscure token.
PROBE_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
PROBE_NAME = "BONK"

DEFAULT_DECIMALS = 9
"""Assumed when the mint account cannot be read, so that the sell check --
the most important one -- still runs."""


@dataclass
class CheckResult:
    """One endpoint check."""

    name: str
    url: str
    ok: bool
    status: str
    latency_ms: float
    detail: str = ""


def _timed_get(url: str, timeout: float, data: bytes | None = None) -> CheckResult:
    """Fetch a URL, timing it and capturing the failure mode."""

    headers = {"User-Agent": "memecoin-bot/0.1"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            elapsed = (time.monotonic() - started) * 1000
            return CheckResult(
                name="", url=url, ok=response.status == 200,
                status=f"HTTP {response.status}", latency_ms=elapsed,
                detail=f"{len(body):,} bytes",
            )
    except urllib.error.HTTPError as error:
        return CheckResult(
            name="", url=url, ok=False, status=f"HTTP {error.code}",
            latency_ms=(time.monotonic() - started) * 1000,
            detail=error.reason or "",
        )
    except Exception as error:  # noqa: BLE001 - report anything as a failure
        return CheckResult(
            name="", url=url, ok=False, status="UNREACHABLE",
            latency_ms=(time.monotonic() - started) * 1000,
            detail=str(error)[:80],
        )


def run_doctor(settings: Settings, mint: str | None = None) -> int:
    """Check every data source and show raw responses beside parsed values."""

    target = mint or PROBE_MINT
    label = PROBE_NAME if target == PROBE_MINT else target[:12] + "..."
    timeout = settings.request_timeout_seconds

    print("=" * 66)
    print("LIVE DATA CHECK")
    print("=" * 66)
    print("Every number this bot reports comes from the three sources below.")
    print("This hits them for real and shows the raw response next to what")
    print("the bot derived from it, so you can check the parsing yourself.")
    print()

    # --- 1. Reachability --------------------------------------------------
    print("1. CONNECTIVITY")
    print("-" * 66)
    checks = [
        ("DexScreener (prices)",
         f"https://api.dexscreener.com/latest/dex/tokens/{target}", None),
        ("Solana RPC (contract state)", settings.rpc_endpoint,
         json.dumps({
             "jsonrpc": "2.0", "id": 1, "method": "getHealth", "params": [],
         }).encode()),
        ("Jupiter (sell routing)",
         f"{settings.jupiter_endpoint}/quote?inputMint={target}"
         f"&outputMint={WRAPPED_SOL}&amount=1000000&slippageBps=300", None),
    ]

    reachable = 0
    for name, url, payload in checks:
        result = _timed_get(url, timeout, payload)
        mark = "OK  " if result.ok else "FAIL"
        print(f"  [{mark}] {name:<30} {result.status:<16} {result.latency_ms:>7.0f}ms")
        if not result.ok and result.detail:
            print(f"         {result.detail}")
        reachable += result.ok

    if reachable == 0:
        print()
        print("  No data source is reachable. Nothing below can run.")
        print("  Check your internet connection or firewall, then retry.")
        return 1
    print()

    # --- 2. Price data: raw vs parsed -------------------------------------
    print(f"2. PRICE DATA for {label}")
    print("-" * 66)
    market = DexScreenerClient(settings)
    snapshot = market.snapshot(target)
    if snapshot is None:
        print("  No pair data returned. The mint may be wrong or have no pool.")
        return 1

    raw = _raw_pair(target, timeout)
    print(f"  {'FIELD':<22}{'RAW FROM API':>20}{'BOT READS AS':>22}")
    rows = [
        ("priceUsd", raw.get("priceUsd"), f"${snapshot.price_usd:.10f}"),
        ("liquidity.usd", _nested(raw, "liquidity", "usd"),
         f"${snapshot.liquidity_usd:,.0f}"),
        ("volume.h24", _nested(raw, "volume", "h24"),
         f"${snapshot.volume_24h_usd:,.0f}"),
        ("priceChange.m5", _nested(raw, "priceChange", "m5"),
         f"{snapshot.price_change_5m:+.2%}"),
        ("txns.m5.buys", _nested(raw, "txns", "m5", "buys"),
         f"{snapshot.buys_5m}"),
        ("txns.m5.sells", _nested(raw, "txns", "m5", "sells"),
         f"{snapshot.sells_5m}"),
    ]
    for field, raw_value, parsed in rows:
        print(f"  {field:<22}{str(raw_value):>20}{parsed:>22}")
    print()
    print("  Note: the API reports priceChange as a percentage (8.4 means")
    print("  +8.4%); the bot converts it to a fraction. That is the only")
    print("  transformation applied to any field above.")
    print()

    # --- 3. Contract state: raw bytes vs decoded --------------------------
    print(f"3. CONTRACT STATE for {label}")
    print("-" * 66)
    rpc = SolanaRpcClient(settings.rpc_endpoint, timeout)
    state = rpc.get_mint_state(target)
    if state is None:
        print("  Could not read the mint account (RPC throttled or blocked).")
        print("  The safety screen treats this as UNKNOWN, which it rejects.")
    else:
        print(f"  owner program          {state.program}")
        print(f"  supply (base units)    {state.supply:,}")
        print(f"  decimals               {state.decimals}")
        print()
        print("  Decoded from the account's first 82 bytes:")
        print(f"    bytes  0..4   mint authority tag   -> revoked: "
              f"{state.mint_authority_revoked}")
        print(f"    bytes 46..50  freeze authority tag -> revoked: "
              f"{state.freeze_authority_revoked}")
        print()
        print("  A tag of 0 means the authority was revoked. Anything else")
        print("  means someone can still mint or freeze, and the bot refuses.")
    print()

    # --- 4. Can it actually be sold? --------------------------------------
    print(f"4. SELL ROUTING for {label}")
    print("-" * 66)
    jupiter = JupiterClient(settings.jupiter_endpoint, timeout)
    # This check must not depend on the RPC read succeeding. Whether a token
    # can be sold at all is the single most important question here, so when
    # decimals are unknown fall back to the Solana default rather than skip.
    decimals = state.decimals if state is not None else DEFAULT_DECIMALS
    if state is None:
        print(f"  (mint decimals unknown; assuming {DEFAULT_DECIMALS})")

    probe = max(1, int(settings.sell_probe_tokens * (10 ** decimals)))
    quote = jupiter.quote(target, WRAPPED_SOL, probe)
    if quote is None:
        print("  No quote returned. Treated as UNKNOWN, which is rejected.")
    else:
        print(f"  selling              {settings.sell_probe_tokens:,.0f} {label}")
        print(f"  route hops           {quote.route_hops}")
        print(f"  price impact         {quote.price_impact_pct:.4%}")
        print(f"  wSOL out (lamports)  {quote.out_amount:,}")
        print()
        verdict = jupiter.can_sell(
            target, probe,
            max_price_impact_pct=settings.max_sell_price_impact_pct,
        )
        print(f"  sellable             {verdict}")
        print("  A route that returns nothing is the honeypot signature.")
    print()

    # --- 5. The bot's verdict ---------------------------------------------
    print(f"5. VERDICT for {label}")
    print("-" * 66)
    provider = OnChainAuthorityProvider(settings, rpc=rpc, jupiter=jupiter)
    authority = provider.fetch(target)
    verdict = screen(snapshot, settings, authority)
    score, notes = score_entry(snapshot, settings)

    print(f"  entry score          {score:.2f}")
    print(f"  tradable             {verdict.passed}")
    for reason in verdict.failures:
        print(f"    - {reason}")
    print()
    print("  Everything above came from a live request made just now.")
    print("  Re-run this any time a number looks wrong.")
    return 0


def _raw_pair(mint: str, timeout: float) -> dict:
    """Fetch the deepest raw pair record, unparsed."""

    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    request = urllib.request.Request(
        url, headers={"User-Agent": "memecoin-bot/0.1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - the caller already reported reachability
        return {}

    pairs = [
        p for p in (document.get("pairs") or [])
        if isinstance(p, dict) and p.get("chainId") == "solana"
    ]
    if not pairs:
        return {}
    return max(pairs, key=lambda p: _nested(p, "liquidity", "usd") or 0)


def _nested(source, *keys):
    """Walk nested dicts, returning None at the first miss."""

    current = source
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
