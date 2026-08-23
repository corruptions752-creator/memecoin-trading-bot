"""Solana RPC client for contract-level token facts.

This reads the two things that decide whether a token can rug you outright:
who still controls the mint, and how concentrated the supply is.

Everything here fails closed. A timeout, an RPC error, an unexpected account
layout, or a malformed response all produce ``None`` -- "not known" -- which
the safety screen treats as a rejection. There is no code path where a failed
lookup becomes a favourable answer.
"""

from dataclasses import dataclass
from typing import Any
import base64
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

# SPL Token mint account layout (82 bytes, little endian).
#
#   0..4    mint_authority COption tag   (0 = None, 1 = Some)
#   4..36   mint_authority pubkey
#   36..44  supply                       u64
#   44      decimals                     u8
#   45      is_initialized               bool
#   46..50  freeze_authority COption tag (0 = None, 1 = Some)
#   50..82  freeze_authority pubkey
#
# A COption tag of 0 means the authority was revoked, which is what we want to
# see. Anything else means someone can still act on the mint.
MINT_ACCOUNT_SIZE = 82
_MINT_AUTHORITY_TAG = slice(0, 4)
_SUPPLY = slice(36, 44)
_DECIMALS = 44
_FREEZE_AUTHORITY_TAG = slice(46, 50)

TOKEN_PROGRAMS = frozenset({
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",       # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",       # Token-2022
})


@dataclass(frozen=True, slots=True)
class MintState:
    """The parsed on-chain state of a token mint."""

    mint_authority_revoked: bool
    freeze_authority_revoked: bool
    supply: int
    decimals: int
    program: str

    @property
    def ui_supply(self) -> float:
        """Supply in whole tokens rather than base units."""

        return self.supply / (10 ** self.decimals) if self.decimals >= 0 else 0.0


class SolanaRpcClient:
    """Minimal JSON-RPC client over the standard library.

    A public RPC endpoint is fine for reads at this request rate. Live trading
    would want a paid endpoint, because public ones rate limit aggressively
    and a throttled safety check is a failed safety check.
    """

    def __init__(self, endpoint: str, timeout: float = 10.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self._request_id = 0

    def _call(self, method: str, params: list[Any]) -> Any | None:
        """Issue one RPC call, returning ``None`` on any failure."""

        self._request_id += 1
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }).encode("utf-8")

        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "memecoin-bot/0.1",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    log.warning("rpc %s returned HTTP %s", method, response.status)
                    return None
                body = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            log.warning("rpc %s failed: %s", method, error)
            return None

        try:
            document = json.loads(body)
        except json.JSONDecodeError:
            log.warning("rpc %s returned invalid JSON", method)
            return None

        if not isinstance(document, dict):
            return None
        if "error" in document:
            log.warning("rpc %s error: %s", method, document["error"])
            return None
        return document.get("result")

    def get_mint_state(self, mint: str) -> MintState | None:
        """Read and parse a token mint account.

        Returns ``None`` when the account is missing, is not a token mint, or
        cannot be parsed -- all of which must block a trade rather than pass.
        """

        result = self._call("getAccountInfo", [
            mint, {"encoding": "base64", "commitment": "confirmed"},
        ])
        if not isinstance(result, dict):
            return None

        value = result.get("value")
        if not isinstance(value, dict):
            log.warning("mint %s does not exist on chain", mint)
            return None

        program = str(value.get("owner") or "")
        if program not in TOKEN_PROGRAMS:
            log.warning("mint %s is not owned by a token program", mint)
            return None

        data = value.get("data")
        if not isinstance(data, list) or len(data) < 2 or data[1] != "base64":
            return None

        try:
            raw = base64.b64decode(data[0], validate=True)
        except (ValueError, TypeError):
            log.warning("mint %s returned undecodable account data", mint)
            return None

        return parse_mint_account(raw, program)

    def get_top_holder_pct(
        self, mint: str, supply: int, *, ignore: frozenset[str] = frozenset()
    ) -> float | None:
        """Largest single holder's share of supply, as a fraction.

        Accounts in ``ignore`` are skipped, which is how liquidity pool
        accounts are excluded -- a pool holding most of the supply is the
        normal, healthy case, not a whale.
        """

        if supply <= 0:
            return None

        result = self._call("getTokenLargestAccounts", [
            mint, {"commitment": "confirmed"},
        ])
        if not isinstance(result, dict):
            return None

        accounts = result.get("value")
        if not isinstance(accounts, list):
            return None

        largest = 0
        for account in accounts:
            if not isinstance(account, dict):
                continue
            if str(account.get("address") or "") in ignore:
                continue
            amount = account.get("amount")
            try:
                held = int(amount)
            except (TypeError, ValueError):
                continue
            largest = max(largest, held)

        if largest <= 0:
            # No holders at all is not a clean bill of health; it means the
            # query told us nothing usable.
            return None
        return largest / supply


def parse_mint_account(raw: bytes, program: str = "") -> MintState | None:
    """Parse the 82-byte SPL mint layout.

    Token-2022 mints carry extensions appended after the base layout, so a
    longer account is accepted as long as the base fields are present.
    """

    if len(raw) < MINT_ACCOUNT_SIZE:
        log.warning("mint account too short: %d bytes", len(raw))
        return None

    mint_tag = int.from_bytes(raw[_MINT_AUTHORITY_TAG], "little")
    freeze_tag = int.from_bytes(raw[_FREEZE_AUTHORITY_TAG], "little")
    supply = int.from_bytes(raw[_SUPPLY], "little")
    decimals = raw[_DECIMALS]

    # A COption tag must be exactly 0 (None) or 1 (Some). Anything else means
    # this is not the layout we think it is, so refuse to interpret it.
    if mint_tag not in (0, 1) or freeze_tag not in (0, 1):
        log.warning("mint account has an unexpected COption tag; refusing")
        return None
    if decimals > 18:
        log.warning("mint account reports implausible decimals: %d", decimals)
        return None

    return MintState(
        mint_authority_revoked=mint_tag == 0,
        freeze_authority_revoked=freeze_tag == 0,
        supply=supply,
        decimals=decimals,
        program=program,
    )
