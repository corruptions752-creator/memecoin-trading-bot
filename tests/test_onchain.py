"""Tests for the on-chain authority provider.

The property under test throughout: a failure anywhere produces "unknown",
and unknown is rejected by the safety screen. There must be no path where a
broken lookup becomes a tradable verdict.
"""

from conftest import make_snapshot

from memecoin_bot.chain import MintState
from memecoin_bot.config import Settings
from memecoin_bot.onchain import OnChainAuthorityProvider, apply_lp_policy
from memecoin_bot.safety import TokenAuthority, screen

SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


class FakeRpc:
    """RPC stub with scriptable answers and a call counter."""

    def __init__(self, state=None, top_holder=None):
        self.state = state
        self.top_holder = top_holder
        self.mint_calls = 0

    def get_mint_state(self, mint):
        self.mint_calls += 1
        return self.state

    def get_top_holder_pct(self, mint, supply, *, ignore=frozenset(),
                           exclude_amount=0, tolerance=0.02):
        return self.top_holder


class FakeJupiter:
    """Jupiter stub returning a fixed sell verdict."""

    def __init__(self, sellable=True):
        self.sellable = sellable
        self.amounts = []

    def can_sell(self, mint, amount, *, max_price_impact_pct=0.15):
        self.amounts.append(amount)
        return self.sellable


def clean_state(**overrides):
    defaults = dict(
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        supply=1_000_000_000_000,
        decimals=6,
        program=SPL_TOKEN,
    )
    defaults.update(overrides)
    return MintState(**defaults)


def build(state=None, top_holder=0.05, sellable=True, settings=None):
    rpc = FakeRpc(state=state, top_holder=top_holder)
    jupiter = FakeJupiter(sellable=sellable)
    provider = OnChainAuthorityProvider(
        settings or Settings(), rpc=rpc, jupiter=jupiter
    )
    return provider, rpc, jupiter


# --- Happy path ----------------------------------------------------------

def test_a_clean_token_reports_verified_facts():
    provider, _, _ = build(state=clean_state())
    authority = provider.fetch("Mint111")

    assert authority.mint_authority_revoked is True
    assert authority.freeze_authority_revoked is True
    assert authority.top_holder_pct == 0.05
    assert authority.sell_simulation_ok is True


def test_lp_lock_is_reported_unknown_not_guessed():
    """It cannot be proven from pair data, so it must not be asserted."""

    provider, _, _ = build(state=clean_state())
    assert provider.fetch("Mint111").lp_locked_or_burned is None


# --- Fail closed ---------------------------------------------------------

def test_an_unreadable_mint_yields_nothing_known():
    provider, _, _ = build(state=None)
    authority = provider.fetch("Mint111")

    assert authority == TokenAuthority()
    assert authority.mint_authority_revoked is None
    assert authority.sell_simulation_ok is None


def test_an_unreadable_mint_is_rejected_by_the_screen():
    provider, _, _ = build(state=None)
    report = screen(make_snapshot(), Settings(), provider.fetch("Mint111"))
    assert not report.passed


def test_a_live_mint_authority_is_surfaced_and_rejected():
    provider, _, _ = build(state=clean_state(mint_authority_revoked=False))
    authority = provider.fetch("Mint111")
    assert authority.mint_authority_revoked is False

    report = screen(make_snapshot(), Settings(), authority)
    assert not report.passed
    assert any("supply can be inflated" in r for r in report.failures)


def test_an_unsellable_token_is_surfaced_and_rejected():
    provider, _, _ = build(state=clean_state(), sellable=False)
    authority = provider.fetch("Mint111")
    assert authority.sell_simulation_ok is False

    report = screen(make_snapshot(), Settings(), authority)
    assert any("honeypot" in r for r in report.failures)


def test_an_unknown_sell_verdict_is_rejected():
    """An outage during the sell check is not a pass."""

    provider, _, _ = build(state=clean_state(), sellable=None)
    authority = provider.fetch("Mint111")
    assert authority.sell_simulation_ok is None
    assert not screen(make_snapshot(), Settings(), authority).passed


def test_unknown_holder_distribution_is_rejected():
    provider, _, _ = build(state=clean_state(), top_holder=None)
    authority = provider.fetch("Mint111")
    assert not screen(make_snapshot(), Settings(), authority).passed


def test_a_whale_is_surfaced():
    provider, _, _ = build(state=clean_state(), top_holder=0.55)
    report = screen(make_snapshot(), Settings(), provider.fetch("Mint111"))
    assert any("55%" in r for r in report.failures)


# --- Probe sizing --------------------------------------------------------

def test_the_sell_probe_uses_a_real_position_size():
    """Quoting dust can succeed where a real position would not."""

    provider, _, jupiter = build(state=clean_state(decimals=6))
    provider.fetch("Mint111")
    assert jupiter.amounts == [1_000 * 10 ** 6]


def test_the_probe_scales_with_decimals():
    provider, _, jupiter = build(state=clean_state(decimals=9))
    provider.fetch("Mint111")
    assert jupiter.amounts == [1_000 * 10 ** 9]


def test_no_sell_check_when_supply_is_zero():
    provider, _, jupiter = build(state=clean_state(supply=0))
    authority = provider.fetch("Mint111")
    assert jupiter.amounts == []
    assert authority.sell_simulation_ok is None


# --- Caching -------------------------------------------------------------

def test_repeated_lookups_are_cached():
    """A scan of many candidates must not re-query the same mint."""

    provider, rpc, _ = build(state=clean_state())
    for _ in range(5):
        provider.fetch("Mint111")
    assert rpc.mint_calls == 1


def test_clearing_the_cache_forces_a_refetch():
    provider, rpc, _ = build(state=clean_state())
    provider.fetch("Mint111")
    provider.clear_cache()
    provider.fetch("Mint111")
    assert rpc.mint_calls == 2


def test_distinct_mints_are_cached_separately():
    provider, rpc, _ = build(state=clean_state())
    provider.fetch("MintA")
    provider.fetch("MintB")
    assert rpc.mint_calls == 2


# --- LP policy -----------------------------------------------------------

def test_strict_policy_leaves_lp_unknown():
    authority = apply_lp_policy(
        TokenAuthority(mint_authority_revoked=True),
        Settings(lp_lock_policy="strict"),
        liquidity_usd=1_000_000.0, age_seconds=86_400,
    )
    assert authority.lp_locked_or_burned is None


def test_substitute_policy_accepts_deep_and_old_pools():
    authority = apply_lp_policy(
        TokenAuthority(mint_authority_revoked=True),
        Settings(lp_lock_policy="substitute"),
        liquidity_usd=1_000_000.0, age_seconds=86_400,
    )
    assert authority.lp_locked_or_burned is True


def test_substitute_policy_still_refuses_thin_pools():
    authority = apply_lp_policy(
        TokenAuthority(),
        Settings(lp_lock_policy="substitute"),
        liquidity_usd=5_000.0, age_seconds=86_400,
    )
    assert authority.lp_locked_or_burned is None


def test_substitute_policy_still_refuses_young_pools():
    authority = apply_lp_policy(
        TokenAuthority(),
        Settings(lp_lock_policy="substitute"),
        liquidity_usd=1_000_000.0, age_seconds=600,
    )
    assert authority.lp_locked_or_burned is None


def test_policy_never_overwrites_a_real_proof():
    authority = apply_lp_policy(
        TokenAuthority(lp_locked_or_burned=False),
        Settings(lp_lock_policy="substitute"),
        liquidity_usd=1_000_000.0, age_seconds=86_400,
    )
    assert authority.lp_locked_or_burned is False


def test_substitute_policy_preserves_the_other_facts():
    """Loosening the LP rule must not launder the checks that did run."""

    authority = apply_lp_policy(
        TokenAuthority(
            mint_authority_revoked=False, sell_simulation_ok=False,
            top_holder_pct=0.9,
        ),
        Settings(lp_lock_policy="substitute"),
        liquidity_usd=1_000_000.0, age_seconds=86_400,
    )
    assert authority.mint_authority_revoked is False
    assert authority.sell_simulation_ok is False
    assert authority.top_holder_pct == 0.9
