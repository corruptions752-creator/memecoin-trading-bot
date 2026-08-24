"""Tests for the Jupiter quote client and the sell-side honeypot check."""

import json
from unittest.mock import patch

from memecoin_bot.jupiter import WRAPPED_SOL, JupiterClient


def respond(payload, status=200):
    """Patch urlopen to return one JSON payload."""

    class FakeResponse:
        def __init__(self):
            self.status = status
        def read(self):
            return json.dumps(payload).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    return patch("urllib.request.urlopen", return_value=FakeResponse())


GOOD_QUOTE = {
    "inputMint": "Mint111",
    "outputMint": WRAPPED_SOL,
    "inAmount": "1000000000",
    "outAmount": "450000000",
    "priceImpactPct": "0.021",
    "routePlan": [{"swapInfo": {}}],
}


def test_a_routable_sell_passes():
    client = JupiterClient()
    with respond(GOOD_QUOTE):
        assert client.can_sell("Mint111", 1_000_000_000) is True


def test_no_route_reads_as_unsellable():
    """Zero out means nothing will buy it -- that is the honeypot signature."""

    client = JupiterClient()
    with respond(dict(GOOD_QUOTE, outAmount="0", routePlan=[])):
        assert client.can_sell("Mint111", 1_000_000_000) is False


def test_ruinous_price_impact_reads_as_unsellable():
    client = JupiterClient()
    with respond(dict(GOOD_QUOTE, priceImpactPct="0.87")):
        assert client.can_sell("Mint111", 1_000_000_000) is False


def test_impact_limit_is_configurable():
    client = JupiterClient()
    with respond(dict(GOOD_QUOTE, priceImpactPct="0.20")):
        assert client.can_sell("Mint111", 1, max_price_impact_pct=0.15) is False
    with respond(dict(GOOD_QUOTE, priceImpactPct="0.20")):
        assert client.can_sell("Mint111", 1, max_price_impact_pct=0.30) is True


def test_an_outage_is_unknown_not_sellable():
    """A failed check must never be read as a pass."""

    client = JupiterClient()
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert client.can_sell("Mint111", 1_000) is None


def test_invalid_json_is_unknown():
    client = JupiterClient()

    class FakeResponse:
        status = 200
        def read(self): return b"<html>"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        assert client.can_sell("Mint111", 1_000) is None


def test_a_zero_amount_is_refused():
    assert JupiterClient().quote("A", "B", 0) is None


def test_quote_parses_route_and_impact():
    client = JupiterClient()
    with respond(GOOD_QUOTE):
        quote = client.quote("Mint111", WRAPPED_SOL, 1_000_000_000)
    assert quote.out_amount == 450_000_000
    assert quote.route_hops == 1
    assert abs(quote.price_impact_pct - 0.021) < 1e-9
    assert quote.has_route


def test_malformed_amounts_are_unknown():
    client = JupiterClient()
    with respond(dict(GOOD_QUOTE, outAmount="not-a-number")):
        assert client.quote("A", "B", 1_000) is None


def test_a_missing_impact_field_defaults_to_zero():
    payload = {k: v for k, v in GOOD_QUOTE.items() if k != "priceImpactPct"}
    client = JupiterClient()
    with respond(payload):
        assert client.quote("A", "B", 1_000).price_impact_pct == 0.0


def test_the_client_never_builds_a_transaction():
    """This module quotes only; nothing here can move funds.

    Checks real code -- string constants and called attribute names -- rather
    than prose, so that a docstring mentioning signing does not trip it.
    """

    import ast
    import inspect
    from memecoin_bot import jupiter

    tree = ast.parse(inspect.getsource(jupiter))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)

    literals = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]

    # Jupiter's current base path is /swap/v1, so the word "swap" appearing
    # in a literal proves nothing. What must not appear is the action that
    # builds a transaction: /swap/v1/swap, as opposed to /swap/v1/quote.
    for text in literals:
        lowered = text.lower()
        assert not lowered.endswith("/swap"), f"swap action referenced: {text!r}"
        assert "swap-instructions" not in lowered, text
        assert "sendtransaction" not in lowered, text

    # And the only endpoint path this client ever builds is a quote. Bare
    # separators like "/" (from rstrip) are not paths.
    paths = [
        x for x in literals
        if x.startswith("/") and len(x) > 1 and not x.startswith("//")
    ]
    for path in paths:
        assert "quote" in path, f"unexpected endpoint path: {path!r}"

    # A quote is a GET. Any request carrying a body would be a state change.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                assert keyword.arg != "data", "jupiter client must not POST"
