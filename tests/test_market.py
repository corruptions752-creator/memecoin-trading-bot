"""Tests for parsing the DexScreener feed.

The feed returns partial and malformed records routinely. A missing field must
never be read as a favourable value, because every one of these numbers feeds
a safety check.
"""

import json
from unittest.mock import patch

from memecoin_bot.config import Settings
from memecoin_bot.market import DexScreenerClient, _number

# Shaped after a real /latest/dex/tokens response.
SAMPLE_PAIR = {
    "chainId": "solana",
    "dexId": "raydium",
    "pairAddress": "PairAddr111",
    "baseToken": {
        "address": "MintAddr111",
        "name": "Test Coin",
        "symbol": "TEST",
    },
    "quoteToken": {"address": "So111...", "symbol": "SOL"},
    "priceUsd": "0.0004521",
    "liquidity": {"usd": 148_233.44, "base": 1e9, "quote": 420.5},
    "fdv": 4_521_000,
    "volume": {"h24": 982_144.2, "h6": 300_000.0, "h1": 88_000.0, "m5": 12_400.0},
    "priceChange": {"m5": 8.4, "h1": 22.7, "h24": -14.2},
    "txns": {"m5": {"buys": 143, "sells": 97}},
    "pairCreatedAt": 1_699_900_000_000,
}


def parse(payload):
    """Run the client's parser over a payload without touching the network."""

    client = DexScreenerClient(Settings())
    with patch.object(DexScreenerClient, "_get", return_value=payload):
        return client.snapshot("MintAddr111")


def test_a_realistic_pair_parses_completely():
    snapshot = parse({"pairs": [SAMPLE_PAIR]})

    assert snapshot is not None
    assert snapshot.mint == "MintAddr111"
    assert snapshot.symbol == "TEST"
    assert snapshot.price_usd == 0.0004521
    assert snapshot.liquidity_usd == 148_233.44
    assert snapshot.volume_24h_usd == 982_144.2
    assert snapshot.buys_5m == 143
    assert snapshot.sells_5m == 97


def test_percentages_are_converted_to_fractions():
    """The feed reports 8.4 for +8.4%; the strategy works in fractions."""

    snapshot = parse({"pairs": [SAMPLE_PAIR]})
    assert abs(snapshot.price_change_5m - 0.084) < 1e-9
    assert abs(snapshot.price_change_1h - 0.227) < 1e-9
    assert abs(snapshot.price_change_24h - (-0.142)) < 1e-9


def test_pair_creation_is_converted_from_milliseconds():
    snapshot = parse({"pairs": [SAMPLE_PAIR]})
    assert snapshot.pair_created_at == 1_699_900_000.0


def test_the_deepest_pool_is_chosen():
    """A trade routes through the deepest pool, so that is the one to quote."""

    shallow = dict(SAMPLE_PAIR, liquidity={"usd": 5_000.0}, pairAddress="Shallow")
    deep = dict(SAMPLE_PAIR, liquidity={"usd": 900_000.0}, pairAddress="Deep")
    snapshot = parse({"pairs": [shallow, deep]})
    assert snapshot.pair_address == "Deep"


def test_other_chains_are_ignored():
    snapshot = parse({"pairs": [dict(SAMPLE_PAIR, chainId="ethereum")]})
    assert snapshot is None


def test_a_pair_without_a_price_is_dropped():
    assert parse({"pairs": [dict(SAMPLE_PAIR, priceUsd=None)]}) is None


def test_a_pair_without_a_mint_is_dropped():
    broken = dict(SAMPLE_PAIR, baseToken={"symbol": "X"})
    assert parse({"pairs": [broken]}) is None


def test_missing_fields_become_zero_not_favourable_values():
    """Absent liquidity must read as zero so the safety screen rejects it."""

    sparse = {
        "chainId": "solana",
        "pairAddress": "P",
        "baseToken": {"address": "M", "symbol": "S"},
        "priceUsd": "0.01",
    }
    snapshot = parse({"pairs": [sparse]})
    assert snapshot is not None
    assert snapshot.liquidity_usd == 0.0
    assert snapshot.volume_24h_usd == 0.0
    assert snapshot.buys_5m == 0
    assert snapshot.pair_created_at == 0.0


def test_garbage_records_do_not_crash_the_parser():
    snapshot = parse({"pairs": [None, "nonsense", 42, [], SAMPLE_PAIR]})
    assert snapshot is not None


def test_an_empty_response_yields_nothing():
    assert parse({"pairs": []}) is None
    assert parse({}) is None


def test_a_failed_request_yields_nothing():
    assert parse(None) is None


def test_number_coercion_handles_junk():
    assert _number("1.5") == 1.5
    assert _number(2) == 2.0
    assert _number(None) == 0.0
    assert _number("abc") == 0.0
    assert _number(True) == 0.0
    assert _number(float("nan")) == 0.0
    assert _number(float("inf")) == 0.0
    assert _number({}) == 0.0


def test_network_errors_are_swallowed_not_raised():
    """A data outage must look like 'no candidates', not kill the loop."""

    client = DexScreenerClient(Settings())
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert client.discover() == []
        assert client.snapshot("MintAddr111") is None


def test_invalid_json_is_swallowed():
    client = DexScreenerClient(Settings())

    class FakeResponse:
        status = 200
        def read(self): return b"<html>not json</html>"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        assert client.discover() == []


# --- Discovery breadth ---------------------------------------------------

def test_discovery_queries_several_sources():
    """A single search returned 16 pairs on a live run and every one was
    rejected. Breadth has to come from combining sources."""

    from unittest.mock import patch as _patch
    client = DexScreenerClient(Settings())
    calls = []

    def record(path):
        calls.append(path)
        return None

    with _patch.object(DexScreenerClient, "_get_json", side_effect=record):
        client.discover()

    assert any("token-boosts" in c for c in calls), "boost feeds not queried"
    assert any("token-profiles" in c for c in calls), "profiles feed not queried"
    searches = [c for c in calls if "search" in c]
    assert len(searches) >= 5, f"only {len(searches)} searches"


def test_discovery_deduplicates_by_mint():
    """The same token surfaces from several searches; it must appear once."""

    from unittest.mock import patch as _patch
    client = DexScreenerClient(Settings())

    with _patch.object(
        DexScreenerClient, "_get_json",
        side_effect=lambda p: [] if ("boosts" in p or "profiles" in p)
        else {"pairs": [SAMPLE_PAIR]},
    ):
        found = client.discover()

    assert len(found) == 1
    assert found[0].mint == "MintAddr111"


def test_discovery_keeps_the_deepest_pool_per_mint():
    """A trade routes through the deepest pool, so that is the one to keep."""

    from unittest.mock import patch as _patch
    shallow = dict(SAMPLE_PAIR, liquidity={"usd": 5_000.0}, pairAddress="Shallow")
    deep = dict(SAMPLE_PAIR, liquidity={"usd": 800_000.0}, pairAddress="Deep")
    client = DexScreenerClient(Settings())

    with _patch.object(
        DexScreenerClient, "_get_json",
        side_effect=lambda p: [] if ("boosts" in p or "profiles" in p)
        else {"pairs": [shallow, deep]},
    ):
        found = client.discover()

    assert len(found) == 1
    assert found[0].pair_address == "Deep"


def test_the_boosted_feed_is_parsed_as_an_array():
    """That endpoint returns a bare JSON array, not an object."""

    from unittest.mock import patch as _patch
    client = DexScreenerClient(Settings())
    boosts = [
        {"chainId": "solana", "tokenAddress": "MintAddr111"},
        {"chainId": "ethereum", "tokenAddress": "0xdeadbeef"},
    ]

    def route(path):
        if "boosts" in path or "profiles" in path:
            return boosts
        return {"pairs": [SAMPLE_PAIR]}

    with _patch.object(DexScreenerClient, "_get_json", side_effect=route):
        found = client.discover()
    assert found


def test_other_chains_are_skipped_in_the_boosted_feed():
    from unittest.mock import patch as _patch
    client = DexScreenerClient(Settings())

    def route(path):
        if "boosts" in path or "profiles" in path:
            return [{"chainId": "ethereum", "tokenAddress": "0xabc"}]
        return {"pairs": []}

    with _patch.object(DexScreenerClient, "_get_json", side_effect=route):
        assert client.discover() == []


def test_a_total_outage_yields_no_candidates_not_an_exception():
    from unittest.mock import patch as _patch
    client = DexScreenerClient(Settings())
    with _patch.object(DexScreenerClient, "_get_json", return_value=None):
        assert client.discover() == []


def test_feed_lookups_are_batched():
    """A hundred one-at-a-time lookups per cycle invites rate limiting and
    makes every scan slow. DexScreener takes 30 addresses per request."""

    from unittest.mock import patch as _patch
    client = DexScreenerClient(Settings())
    mints = [f"Mint{i:03d}" for i in range(90)]
    requested = []

    def route(path):
        if "boosts" in path or "profiles" in path:
            return [
                {"chainId": "solana", "tokenAddress": m} for m in mints
            ]
        requested.append(path)
        return {"pairs": []}

    with _patch.object(DexScreenerClient, "_get_json", side_effect=route):
        client.discover()

    batched = [p for p in requested if "/latest/dex/tokens/" in p]
    assert batched, "no batched token lookups"
    assert len(batched) <= 4, f"{len(batched)} requests for 90 mints"
    assert "%2C" in batched[0] or "," in batched[0], "addresses not comma-joined"


def test_feed_tokens_are_capped():
    """A runaway feed must not turn one cycle into hundreds of requests."""

    from unittest.mock import patch as _patch
    client = DexScreenerClient(Settings())
    huge = [
        {"chainId": "solana", "tokenAddress": f"Mint{i:04d}"}
        for i in range(5_000)
    ]
    lookups = []

    def route(path):
        if "boosts" in path or "profiles" in path:
            return huge
        lookups.append(path)
        return {"pairs": []}

    with _patch.object(DexScreenerClient, "_get_json", side_effect=route):
        client.discover()

    token_calls = [p for p in lookups if "/latest/dex/tokens/" in p]
    assert len(token_calls) <= DexScreenerClient._MAX_FEED_TOKENS // 30 + 1


def test_duplicate_mints_across_feeds_are_looked_up_once():
    from unittest.mock import patch as _patch
    client = DexScreenerClient(Settings())
    same = [{"chainId": "solana", "tokenAddress": "MintAddr111"}]
    lookups = []

    def route(path):
        if "boosts" in path or "profiles" in path:
            return same
        lookups.append(path)
        return {"pairs": []}

    with _patch.object(DexScreenerClient, "_get_json", side_effect=route):
        client.discover()

    token_calls = [p for p in lookups if "/latest/dex/tokens/" in p]
    assert len(token_calls) == 1
