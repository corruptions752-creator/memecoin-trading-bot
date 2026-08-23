"""Tests for the pre-trade safety screen."""

from conftest import NOW, make_snapshot, safe_authority

from memecoin_bot.safety import TokenAuthority, screen


def test_clean_token_passes_full_screen(settings):
    report = screen(make_snapshot(), settings, safe_authority())
    assert report.passed, report.failures


def test_unknown_contract_state_is_rejected(settings):
    """The default 'we know nothing' provider must never yield a pass."""

    report = screen(make_snapshot(), settings, TokenAuthority())
    assert not report.passed
    assert any("mint authority" in reason for reason in report.failures)
    assert any("honeypot" in reason for reason in report.failures)


def test_missing_authority_argument_is_rejected(settings):
    report = screen(make_snapshot(), settings)
    assert not report.passed


def test_live_mint_authority_blocks_entry(settings):
    report = screen(
        make_snapshot(), settings, safe_authority(mint_authority_revoked=False)
    )
    assert not report.passed
    assert any("supply can be inflated" in r for r in report.failures)


def test_failed_sell_simulation_blocks_entry(settings):
    report = screen(
        make_snapshot(), settings, safe_authority(sell_simulation_ok=False)
    )
    assert not report.passed
    assert any("honeypot" in r for r in report.failures)


def test_whale_concentration_blocks_entry(settings):
    report = screen(
        make_snapshot(), settings, safe_authority(top_holder_pct=0.45)
    )
    assert not report.passed
    assert any("45%" in r for r in report.failures)


def test_thin_liquidity_rejected(settings):
    report = screen(
        make_snapshot(liquidity_usd=1_000.0), settings, safe_authority()
    )
    assert not report.passed
    assert any("liquidity" in r for r in report.failures)


def test_brand_new_pair_rejected(settings):
    """The first half hour is where the instant rugs live."""

    report = screen(
        make_snapshot(pair_created_at=NOW - 120), settings, safe_authority()
    )
    assert not report.passed
    assert any("old, minimum" in r for r in report.failures)


def test_stale_pair_rejected(settings):
    report = screen(
        make_snapshot(pair_created_at=NOW - 30 * 86_400),
        settings,
        safe_authority(),
    )
    assert not report.passed


def test_unknown_pair_age_rejected(settings):
    report = screen(
        make_snapshot(pair_created_at=0), settings, safe_authority()
    )
    assert not report.passed
    assert any("age unknown" in r for r in report.failures)


def test_wash_trading_turnover_rejected(settings):
    report = screen(
        make_snapshot(liquidity_usd=30_000.0, volume_24h_usd=100_000_000.0),
        settings,
        safe_authority(),
    )
    assert not report.passed
    assert any("wash trading" in r for r in report.failures)


def test_heavy_sell_pressure_rejected(settings):
    report = screen(
        make_snapshot(buys_5m=10, sells_5m=200), settings, safe_authority()
    )
    assert not report.passed
    assert any("sell pressure" in r for r in report.failures)


def test_market_only_mode_downgrades_contract_checks_to_warnings(settings):
    """Paper mode may run without an on-chain provider, but must say so."""

    report = screen(
        make_snapshot(),
        settings,
        TokenAuthority(),
        require_contract_checks=False,
    )
    assert report.passed
    assert report.warnings
    assert all(w.startswith("unenforced:") for w in report.warnings)


def test_market_failures_still_apply_without_contract_checks(settings):
    report = screen(
        make_snapshot(liquidity_usd=10.0),
        settings,
        safe_authority(),
        require_contract_checks=False,
    )
    assert not report.passed


def test_describe_is_readable(settings):
    report = screen(make_snapshot(liquidity_usd=1.0), settings, safe_authority())
    assert "rejected" in report.describe()
