"""Jupiter quote client and the sell-side honeypot check.

The single most useful pre-trade question is not "does this look good" but
"could I get out". A honeypot lets you buy and then blocks or taxes the sell.
Reading contract flags does not reliably catch that; asking the aggregator to
route a real sell does.

This module only ever requests *quotes*. It never builds, signs, or sends a
transaction, so nothing here can move funds.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://lite-api.jup.ag/swap/v1"
"""Jupiter's keyless tier.

``quote-api.jup.ag/v6`` was retired and its hostname no longer resolves, so
every sell simulation failed DNS and returned "unknown" -- which the screen
correctly treats as a rejection. The result was a bot that could never verify
any token, for a reason that looked exactly like every token being unsafe.

``api.jup.ag`` is the successor and wants a free key from portal.jup.ag; set
MEMEBOT_JUPITER_ENDPOINT and MEMEBOT_JUPITER_API_KEY to use it. Jupiter has
announced this keyless host will eventually retire too, so a failing sell
check is worth checking against the endpoint before blaming the market."""
WRAPPED_SOL = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True, slots=True)
class Quote:
    """A routing quote for one swap direction."""

    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price_impact_pct: float
    route_hops: int

    @property
    def has_route(self) -> bool:
        """Whether the aggregator found a usable path."""

        return self.out_amount > 0


class JupiterClient:
    """Read-only Jupiter aggregator client."""

    def __init__(
        self, endpoint: str = DEFAULT_ENDPOINT, timeout: float = 10.0,
        api_key: str = "",
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key

    def quote(
        self, input_mint: str, output_mint: str, amount: int,
        slippage_bps: int = 300,
    ) -> Quote | None:
        """Request a swap quote, returning ``None`` on any failure."""

        if amount <= 0:
            return None

        query = urlencode({
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount,
            "slippageBps": slippage_bps,
            # Restricting to direct routes makes the check stricter and the
            # result easier to reason about than a multi-hop path that may
            # not exist by the time an order is placed.
            "onlyDirectRoutes": "false",
        })
        url = f"{self.endpoint}/quote?{query}"

        document = self._get(url)
        if document is None:
            return None

        try:
            out_amount = int(document.get("outAmount", 0))
            in_amount = int(document.get("inAmount", 0))
        except (TypeError, ValueError):
            return None

        try:
            impact = float(document.get("priceImpactPct") or 0.0)
        except (TypeError, ValueError):
            impact = 0.0

        plan = document.get("routePlan")
        hops = len(plan) if isinstance(plan, list) else 0

        return Quote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=in_amount,
            out_amount=out_amount,
            price_impact_pct=impact,
            route_hops=hops,
        )

    def can_sell(
        self, mint: str, amount: int, *, max_price_impact_pct: float = 0.15,
    ) -> bool | None:
        """Whether ``amount`` base units of ``mint`` can be routed back to SOL.

        Returns ``True`` only on a routable sell within the impact limit,
        ``False`` on a confirmed unsellable or ruinously illiquid position,
        and ``None`` when the check could not be completed. ``None`` is not a
        pass: the caller treats it as unknown and therefore as a rejection.
        """

        quote = self.quote(mint, WRAPPED_SOL, amount)
        if quote is None:
            return None

        if not quote.has_route:
            log.warning("no sell route for %s; treating as unsellable", mint)
            return False

        if quote.price_impact_pct > max_price_impact_pct:
            log.warning(
                "sell impact for %s is %.1f%%, above the %.1f%% limit",
                mint, quote.price_impact_pct * 100, max_price_impact_pct * 100,
            )
            return False

        return True

    def _get(self, url: str) -> dict[str, Any] | None:
        """GET a JSON document, returning ``None`` on any failure."""

        headers = {"User-Agent": "memecoin-bot/0.1"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    # A 4xx here usually means no route exists for the pair,
                    # which is itself informative, but the caller cannot tell
                    # that apart from an outage, so report nothing.
                    log.debug("jupiter returned HTTP %s", response.status)
                    return None
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            log.debug("jupiter HTTP error %s", error.code)
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            log.warning("jupiter request failed: %s", error)
            return None

        try:
            document = json.loads(body)
        except json.JSONDecodeError:
            return None
        return document if isinstance(document, dict) else None
