"""Tests for the command line interface.

The network is unavailable in CI, so these drive the commands with fakes and
check the rendered output rather than hitting live endpoints.
"""

from unittest.mock import patch

from conftest import FakeMarket, make_snapshot

from memecoin_bot.__main__ import _verify, main
from memecoin_bot.chain import MintState
from memecoin_bot.config import Settings

SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
MINT = "MintAddress1111111111111111111111111111111"


class StubProvider:
    """Stands in for the on-chain provider."""

    def __init__(self, authority):
        self.authority = authority

    def fetch(self, mint):
        return self.authority


def run_verify(authority, snapshot=None, settings=None):
    """Drive _verify with a stubbed provider and capture printed output."""

    snapshot = snapshot or make_snapshot(mint=MINT)
    market = FakeMarket(prices={MINT: snapshot})
    lines = []

    import logging
    with patch("memecoin_bot.__main__.OnChainAuthorityProvider",
               return_value=StubProvider(authority)), \
         patch("builtins.print", side_effect=lambda *a: lines.append(
             " ".join(str(x) for x in a))):
        code = _verify(
            settings or Settings(), market, MINT, logging.getLogger("test")
        )
    return code, "\n".join(lines)


def clean_authority(**overrides):
    from memecoin_bot.safety import TokenAuthority
    defaults = dict(
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        lp_locked_or_burned=True,
        top_holder_pct=0.05,
        sell_simulation_ok=True,
    )
    defaults.update(overrides)
    return TokenAuthority(**defaults)


def test_verify_renders_a_tradable_verdict():
    code, output = run_verify(clean_authority())
    assert code == 0
    assert "VERDICT" in output
    assert "TRADABLE" in output
    assert "PASS" in output


def test_verify_shows_unknown_for_unverifiable_checks():
    """UNKNOWN must be visible, not silently rendered as a pass.

    Pinned to strict, because paper mode now resolves to the substitute
    policy -- otherwise depth and age would fill the LP slot in and there
    would be no UNKNOWN left to display.
    """

    code, output = run_verify(
        clean_authority(lp_locked_or_burned=None),
        settings=Settings(lp_lock_policy="strict"),
    )
    assert "UNKNOWN" in output
    assert "REJECTED" in output


def test_verify_in_paper_mode_substitutes_the_lp_check():
    """Paper defaults to substitute so the bot can actually trade."""

    code, output = run_verify(clean_authority(lp_locked_or_burned=None))
    assert "TRADABLE" in output


def test_verify_lists_the_reasons_for_rejection():
    code, output = run_verify(clean_authority(sell_simulation_ok=False))
    assert "REJECTED" in output
    assert "honeypot" in output
    assert "FAIL" in output


def test_verify_shows_a_whale_share():
    code, output = run_verify(clean_authority(top_holder_pct=0.62))
    assert "62.0%" in output
    assert "REJECTED" in output


def test_verify_reports_the_fail_closed_rule():
    _, output = run_verify(clean_authority())
    assert "never treated as a pass" in output


def test_verify_handles_a_missing_pair():
    import logging
    market = FakeMarket(prices={})
    code = _verify(Settings(), market, "Nope", logging.getLogger("test"))
    assert code == 1


def test_verify_without_a_mint_is_a_usage_error():
    assert main(["verify"]) == 2


def test_an_invalid_mode_exits_cleanly(monkeypatch):
    monkeypatch.setenv("MEMEBOT_MODE", "nonsense")
    assert main(["scan"]) == 2


def test_simulate_runs_from_the_cli(capsys):
    assert main(["simulate"]) == 0
    assert "SIMULATION" in capsys.readouterr().out


def test_sweep_runs_from_the_cli(capsys):
    assert main(["sweep"]) == 0
    assert "Losing runs" in capsys.readouterr().out
