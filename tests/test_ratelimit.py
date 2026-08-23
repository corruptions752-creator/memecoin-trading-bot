"""Tests for rate limiting and retry.

A throttled safety check fails closed, so the bot goes quiet and looks like it
is seeing a dull market. These make sure a rate limit cannot masquerade as an
empty screen.
"""

import random
from unittest.mock import patch
import urllib.error

from memecoin_bot.chain import SolanaRpcClient
from memecoin_bot.ratelimit import TokenBucket, backoff_delays


# --- Token bucket --------------------------------------------------------

def test_a_full_bucket_does_not_wait():
    bucket = TokenBucket(rate=10.0, capacity=5.0)
    assert bucket.acquire(sleeper=lambda s: None) == 0.0


def test_the_burst_capacity_is_respected():
    bucket = TokenBucket(rate=1.0, capacity=3.0)
    waits = [bucket.acquire(sleeper=lambda s: None) for _ in range(3)]
    assert all(w == 0.0 for w in waits)


def test_exceeding_the_burst_forces_a_wait():
    bucket = TokenBucket(rate=2.0, capacity=2.0)
    for _ in range(2):
        bucket.acquire(sleeper=lambda s: None)
    assert bucket.acquire(sleeper=lambda s: None) > 0


def test_the_wait_matches_the_configured_rate():
    bucket = TokenBucket(rate=4.0, capacity=1.0)
    bucket.acquire(sleeper=lambda s: None)
    wait = bucket.acquire(sleeper=lambda s: None)
    assert abs(wait - 0.25) < 0.05


# --- Backoff -------------------------------------------------------------

def test_backoff_grows_exponentially():
    ceilings = [0.5, 1.0, 2.0, 4.0]
    delays = backoff_delays(4, base=0.5, jitter=random.Random(1))
    for delay, ceiling in zip(delays, ceilings):
        assert 0.0 <= delay <= ceiling


def test_backoff_is_capped():
    delays = backoff_delays(10, base=0.5, cap=3.0, jitter=random.Random(1))
    assert all(d <= 3.0 for d in delays)


def test_backoff_is_jittered():
    """Unjittered retries re-create the overload that caused the outage."""

    a = backoff_delays(5, jitter=random.Random(1))
    b = backoff_delays(5, jitter=random.Random(2))
    assert a != b


# --- Retry behaviour -----------------------------------------------------

def http_error(code):
    return urllib.error.HTTPError("u", code, "err", {}, None)


def test_a_throttled_request_is_retried():
    client = SolanaRpcClient("https://rpc.example", max_retries=2)
    calls = []

    class Ok:
        status = 200
        def read(self): return b'{"jsonrpc":"2.0","id":1,"result":{"value":null}}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise http_error(429)
        return Ok()

    with patch("urllib.request.urlopen", side_effect=flaky), \
         patch("time.sleep"):
        client.get_mint_state("Mint111")
    assert len(calls) == 3


def test_retries_are_bounded():
    client = SolanaRpcClient("https://rpc.example", max_retries=2)
    calls = []

    def always_throttled(*args, **kwargs):
        calls.append(1)
        raise http_error(429)

    with patch("urllib.request.urlopen", side_effect=always_throttled), \
         patch("time.sleep"):
        assert client.get_mint_state("Mint111") is None
    assert len(calls) == 3


def test_a_client_error_is_not_retried():
    """A 400 is a real refusal; retrying it just wastes the rate limit."""

    client = SolanaRpcClient("https://rpc.example", max_retries=3)
    calls = []

    def bad_request(*args, **kwargs):
        calls.append(1)
        raise http_error(400)

    with patch("urllib.request.urlopen", side_effect=bad_request), \
         patch("time.sleep"):
        assert client.get_mint_state("Mint111") is None
    assert len(calls) == 1


def test_server_errors_are_retried():
    client = SolanaRpcClient("https://rpc.example", max_retries=1)
    calls = []

    def server_error(*args, **kwargs):
        calls.append(1)
        raise http_error(503)

    with patch("urllib.request.urlopen", side_effect=server_error), \
         patch("time.sleep"):
        client.get_mint_state("Mint111")
    assert len(calls) == 2


def test_exhausted_retries_still_fail_closed():
    """However it fails, the answer must be 'unknown', never a pass."""

    client = SolanaRpcClient("https://rpc.example", max_retries=1)
    with patch("urllib.request.urlopen", side_effect=OSError("down")), \
         patch("time.sleep"):
        assert client.get_mint_state("Mint111") is None
