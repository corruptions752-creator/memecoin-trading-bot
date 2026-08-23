"""Tests for the Solana RPC client and SPL mint account parsing.

The mint layout is a fixed binary format, so these build real account bytes
and check that the parser reads them the way the chain writes them.
"""

import base64
from unittest.mock import patch

from memecoin_bot.chain import (
    MINT_ACCOUNT_SIZE,
    SolanaRpcClient,
    parse_mint_account,
)

SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def build_mint(
    *, mint_authority: bool, freeze_authority: bool,
    supply: int = 1_000_000_000_000_000, decimals: int = 6,
    extra: bytes = b"",
) -> bytes:
    """Construct a real 82-byte SPL mint account.

    ``mint_authority=True`` means an authority is still present (COption tag
    1), which is the dangerous case.
    """

    raw = bytearray(MINT_ACCOUNT_SIZE)
    raw[0:4] = (1 if mint_authority else 0).to_bytes(4, "little")
    raw[4:36] = bytes(range(32)) if mint_authority else bytes(32)
    raw[36:44] = supply.to_bytes(8, "little")
    raw[44] = decimals
    raw[45] = 1  # is_initialized
    raw[46:50] = (1 if freeze_authority else 0).to_bytes(4, "little")
    raw[50:82] = bytes(range(32)) if freeze_authority else bytes(32)
    return bytes(raw) + extra


# --- Parsing -------------------------------------------------------------

def test_a_safe_mint_reads_as_revoked():
    state = parse_mint_account(
        build_mint(mint_authority=False, freeze_authority=False)
    )
    assert state is not None
    assert state.mint_authority_revoked is True
    assert state.freeze_authority_revoked is True


def test_a_live_mint_authority_is_detected():
    """Supply can still be printed; this must never read as safe."""

    state = parse_mint_account(
        build_mint(mint_authority=True, freeze_authority=False)
    )
    assert state.mint_authority_revoked is False
    assert state.freeze_authority_revoked is True


def test_a_live_freeze_authority_is_detected():
    state = parse_mint_account(
        build_mint(mint_authority=False, freeze_authority=True)
    )
    assert state.mint_authority_revoked is True
    assert state.freeze_authority_revoked is False


def test_supply_and_decimals_round_trip():
    state = parse_mint_account(
        build_mint(
            mint_authority=False, freeze_authority=False,
            supply=42_000_000_000, decimals=9,
        )
    )
    assert state.supply == 42_000_000_000
    assert state.decimals == 9
    assert state.ui_supply == 42.0


def test_token_2022_extensions_are_tolerated():
    """Extensions append after the base layout; the base fields still parse."""

    state = parse_mint_account(
        build_mint(
            mint_authority=False, freeze_authority=False, extra=b"\x01" * 200
        )
    )
    assert state is not None
    assert state.mint_authority_revoked is True


def test_a_truncated_account_is_refused():
    assert parse_mint_account(b"\x00" * 40) is None


def test_an_impossible_option_tag_is_refused():
    """A tag that is neither 0 nor 1 means this is not the layout we expect."""

    raw = bytearray(build_mint(mint_authority=False, freeze_authority=False))
    raw[0:4] = (7).to_bytes(4, "little")
    assert parse_mint_account(bytes(raw)) is None


def test_implausible_decimals_are_refused():
    raw = bytearray(build_mint(mint_authority=False, freeze_authority=False))
    raw[44] = 200
    assert parse_mint_account(bytes(raw)) is None


# --- RPC -----------------------------------------------------------------

def rpc_with(result):
    """A client whose single RPC call returns ``result``."""

    client = SolanaRpcClient("https://rpc.example")
    return client, patch.object(SolanaRpcClient, "_call", return_value=result)


def account_payload(raw: bytes, owner: str = SPL_TOKEN):
    """Shape a getAccountInfo response around raw account bytes."""

    return {
        "value": {
            "owner": owner,
            "data": [base64.b64encode(raw).decode(), "base64"],
            "lamports": 1_000_000,
        }
    }


def test_get_mint_state_decodes_a_real_response():
    raw = build_mint(mint_authority=False, freeze_authority=False)
    client, patched = rpc_with(account_payload(raw))
    with patched:
        state = client.get_mint_state("Mint111")
    assert state is not None
    assert state.mint_authority_revoked is True
    assert state.program == SPL_TOKEN


def test_a_missing_account_returns_unknown():
    client, patched = rpc_with({"value": None})
    with patched:
        assert client.get_mint_state("Mint111") is None


def test_an_account_owned_by_another_program_is_refused():
    """Something that is not a token mint must not be parsed as one."""

    raw = build_mint(mint_authority=False, freeze_authority=False)
    client, patched = rpc_with(account_payload(raw, owner="SomeOtherProgram"))
    with patched:
        assert client.get_mint_state("Mint111") is None


def test_undecodable_data_returns_unknown():
    client, patched = rpc_with({
        "value": {"owner": SPL_TOKEN, "data": ["!!!not base64!!!", "base64"]}
    })
    with patched:
        assert client.get_mint_state("Mint111") is None


def test_an_rpc_failure_returns_unknown():
    """A failed lookup must never become a favourable answer."""

    client, patched = rpc_with(None)
    with patched:
        assert client.get_mint_state("Mint111") is None
        assert client.get_top_holder_pct("Mint111", 1_000) is None


def test_network_errors_are_swallowed():
    client = SolanaRpcClient("https://rpc.example")
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert client.get_mint_state("Mint111") is None


def test_an_rpc_error_response_returns_unknown():
    client = SolanaRpcClient("https://rpc.example")

    class FakeResponse:
        status = 200
        def read(self): return b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601}}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        assert client.get_mint_state("Mint111") is None


# --- Holder concentration ------------------------------------------------

def test_top_holder_share_is_computed():
    client, patched = rpc_with({
        "value": [
            {"address": "Whale", "amount": "300"},
            {"address": "Other", "amount": "100"},
        ]
    })
    with patched:
        assert client.get_top_holder_pct("Mint111", 1_000) == 0.30


def test_pool_accounts_can_be_excluded():
    """A pool holding most of the supply is normal, not a whale."""

    client, patched = rpc_with({
        "value": [
            {"address": "Pool", "amount": "900"},
            {"address": "Holder", "amount": "50"},
        ]
    })
    with patched:
        share = client.get_top_holder_pct(
            "Mint111", 1_000, ignore=frozenset({"Pool"})
        )
    assert share == 0.05


def test_unparsable_amounts_are_skipped():
    client, patched = rpc_with({
        "value": [
            {"address": "Bad", "amount": None},
            {"address": "Good", "amount": "250"},
        ]
    })
    with patched:
        assert client.get_top_holder_pct("Mint111", 1_000) == 0.25


def test_no_holders_reads_as_unknown_not_zero():
    """An empty result told us nothing; it is not a clean bill of health."""

    client, patched = rpc_with({"value": []})
    with patched:
        assert client.get_top_holder_pct("Mint111", 1_000) is None


def test_zero_supply_reads_as_unknown():
    client, patched = rpc_with({"value": [{"address": "A", "amount": "1"}]})
    with patched:
        assert client.get_top_holder_pct("Mint111", 0) is None
