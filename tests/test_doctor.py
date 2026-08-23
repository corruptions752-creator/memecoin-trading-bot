"""Tests for the live data check.

The sandbox that builds this has no network, so these drive the doctor with
recorded response shapes. What they verify is the thing that actually matters:
that the "raw" column and the "parsed" column are consistent, so the display
cannot quietly lie about what the API returned.
"""

import json
from unittest.mock import patch

from memecoin_bot.config import Settings
from memecoin_bot.doctor import PROBE_MINT, run_doctor

PAIR = {
    "chainId": "solana",
    "pairAddress": "PairAddr111",
    "baseToken": {"address": PROBE_MINT, "name": "Bonk", "symbol": "BONK"},
    "priceUsd": "0.00002841",
    "liquidity": {"usd": 4_812_004.55},
    "fdv": 1_900_000_000,
    "volume": {"h24": 38_221_907.4, "m5": 41_002.1},
    "priceChange": {"m5": 1.8, "h1": -3.2, "h24": 12.4},
    "txns": {"m5": {"buys": 312, "sells": 288}},
    "pairCreatedAt": 1_672_531_200_000,
}

QUOTE = {
    "inAmount": "1000000000",
    "outAmount": "182004411",
    "priceImpactPct": "0.0021",
    "routePlan": [{"swapInfo": {}}, {"swapInfo": {}}],
}


def fake_urlopen(url_router):
    """Patch urlopen, dispatching on substring of the requested URL."""

    class Response:
        def __init__(self, payload, status=200):
            self._payload = json.dumps(payload).encode()
            self.status = status
        def read(self): return self._payload
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        for fragment, payload in url_router.items():
            if fragment in url:
                return Response(payload)
        return Response({}, status=404)

    return patch("urllib.request.urlopen", side_effect=opener)


def run(capsys, router=None, mint=None):
    """Run the doctor against mocked endpoints and capture its output."""

    router = router if router is not None else {
        "dexscreener": {"pairs": [PAIR]},
        "mainnet-beta": {"result": "ok"},
        "jup.ag": QUOTE,
    }
    with fake_urlopen(router):
        code = run_doctor(Settings(), mint)
    return code, capsys.readouterr().out


def test_all_sources_reachable_is_reported(capsys):
    _, out = run(capsys)
    assert out.count("[OK  ]") >= 2
    assert "CONNECTIVITY" in out


def test_a_total_outage_stops_early_and_says_so(capsys):
    code, out = run(capsys, router={})
    assert code == 1
    assert "No data source is reachable" in out


def test_the_raw_price_appears_beside_the_parsed_price(capsys):
    """The display must show the API's own string, not only our float."""

    _, out = run(capsys)
    assert "0.00002841" in out
    assert "priceUsd" in out


def test_raw_liquidity_and_volume_are_shown(capsys):
    _, out = run(capsys)
    assert "4812004.55" in out or "4,812,004" in out
    assert "liquidity.usd" in out
    assert "volume.h24" in out


def test_the_percentage_conversion_is_disclosed(capsys):
    """The one transformation applied to any field must be stated."""

    _, out = run(capsys)
    assert "1.8" in out          # the raw value
    assert "+1.80%" in out       # what the bot reads it as
    assert "percentage" in out


def test_raw_transaction_counts_are_shown(capsys):
    _, out = run(capsys)
    assert "312" in out
    assert "288" in out


def test_the_sell_route_is_reported(capsys):
    _, out = run(capsys)
    assert "SELL ROUTING" in out
    assert "182,004,411" in out or "182004411" in out


def test_a_verdict_is_printed(capsys):
    _, out = run(capsys)
    assert "VERDICT" in out
    assert "entry score" in out


def test_an_unreadable_mint_is_reported_as_unknown(capsys):
    """RPC failure must surface as UNKNOWN, never as a silent pass."""

    _, out = run(capsys, router={
        "dexscreener": {"pairs": [PAIR]},
        "jup.ag": QUOTE,
    })
    assert "UNKNOWN" in out or "Could not read the mint account" in out


def test_a_missing_pair_exits_with_an_error(capsys):
    code, out = run(capsys, router={
        "dexscreener": {"pairs": []},
        "mainnet-beta": {"result": "ok"},
        "jup.ag": QUOTE,
    })
    assert code == 1
    assert "No pair data" in out


def test_a_custom_mint_is_honoured(capsys):
    custom = "So11111111111111111111111111111111111111112"
    pair = dict(PAIR, baseToken={"address": custom, "symbol": "WSOL"})
    _, out = run(capsys, router={
        "dexscreener": {"pairs": [pair]},
        "mainnet-beta": {"result": "ok"},
        "jup.ag": QUOTE,
    }, mint=custom)
    assert custom[:12] in out
