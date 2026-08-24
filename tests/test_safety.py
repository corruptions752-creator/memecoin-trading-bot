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


def test_a_month_old_pair_is_a_normal_candidate(settings):
    """Age is not a disqualifier; see test_age_alone_no_longer_rejects."""

    report = screen(
        make_snapshot(pair_created_at=NOW - 30 * 86_400),
        settings,
        safe_authority(),
    )
    assert report.passed, report.failures


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


def test_an_established_token_is_not_rejected_for_age(settings):
    """A pair's age is not its momentum.

    A one-week cap rejected 67 of 151 pairs on a live scan, for the stated
    reason that they were 'past the momentum window'. Momentum is measured
    directly from recent price moves, so an established token making a sharp
    move is a valid candidate.
    """

    month_old = make_snapshot(pair_created_at=NOW - 30 * 86_400)
    report = screen(month_old, settings, safe_authority())
    assert report.passed, report.failures


def test_age_alone_no_longer_rejects_anything(settings):
    """Every risk an age cap stood in for is checked directly elsewhere."""

    from memecoin_bot.config import Settings

    old_but_healthy = make_snapshot(pair_created_at=NOW - 900 * 86_400)
    assert screen(old_but_healthy, settings, safe_authority()).passed


def test_the_age_cap_can_be_re_enabled():
    from memecoin_bot.config import Settings

    capped = Settings(max_pair_age_seconds=7 * 86_400)
    report = screen(
        make_snapshot(pair_created_at=NOW - 30 * 86_400),
        capped, safe_authority(),
    )
    assert not report.passed
    assert any("abandoned" in r for r in report.failures)


def test_an_old_dead_pool_is_still_rejected_on_its_merits(settings):
    """Not by age -- by the liquidity and volume floors that mean 'dead'."""

    report = screen(
        make_snapshot(
            pair_created_at=NOW - 900 * 86_400, liquidity_usd=200.0,
            volume_24h_usd=50.0,
        ),
        settings, safe_authority(),
    )
    assert not report.passed
    assert any("liquidity" in r for r in report.failures)


def test_the_minimum_age_still_blocks_fresh_launches(settings):
    """Loosening the upper bound must not touch the rug guard."""

    report = screen(
        make_snapshot(pair_created_at=NOW - 300), settings, safe_authority()
    )
    assert not report.passed
    assert any("old, minimum" in r for r in report.failures)


def test_every_failure_message_has_a_category():
    """A miscategorised rejection shows as 'other' on the dashboard, which
    hides why the bot is not trading.

    This regressed once already: the age message was reworded and the
    categoriser was not, so 37 of 150 rejections on a live scan became
    'other'. Driving real failures through the categoriser catches that.
    """

    from memecoin_bot.safety import categorize

    provokers = [
        make_snapshot(price_usd=0.0),
        make_snapshot(liquidity_usd=1.0),
        make_snapshot(volume_24h_usd=1.0),
        make_snapshot(pair_created_at=0),
        make_snapshot(pair_created_at=NOW - 60),
        make_snapshot(pair_created_at=NOW - 900 * 86_400),
        make_snapshot(fdv_usd=9e12),
        make_snapshot(liquidity_usd=30_000.0, volume_24h_usd=9e9),
        make_snapshot(buys_5m=1, sells_5m=999),
    ]
    seen = set()
    for snapshot in provokers:
        report = screen(snapshot, settings_for_test(), safe_authority())
        for failure in report.failures:
            bucket = categorize(failure)
            assert bucket != "other", f"uncategorised: {failure!r}"
            seen.add(bucket)

    # And the contract-side messages.
    from memecoin_bot.safety import TokenAuthority
    blank = screen(make_snapshot(), settings_for_test(), TokenAuthority())
    for failure in blank.failures:
        assert categorize(failure) != "other", f"uncategorised: {failure!r}"

    assert len(seen) >= 6


def settings_for_test():
    from memecoin_bot.config import Settings
    return Settings()


# --- Supply overhang -----------------------------------------------------

def test_a_thin_small_cap_is_rejected_for_overhang(settings):
    """The flat cap waved this through: small enough to pass on size, but
    750x more token value in existence than the pool could absorb."""

    report = screen(
        make_snapshot(fdv_usd=30_000_000.0, liquidity_usd=40_000.0),
        settings, safe_authority(),
    )
    assert not report.passed
    assert any("overhang" in r for r in report.failures)


def test_a_large_token_on_a_deep_pool_passes(settings):
    """Rejected by the flat cap despite carrying the same 100x overhang as
    a small token the cap allowed."""

    report = screen(
        make_snapshot(fdv_usd=250_000_000.0, liquidity_usd=2_500_000.0),
        settings, safe_authority(),
    )
    assert report.passed, report.failures


def test_the_size_cap_still_applies(settings):
    """Above it a same-session 2x is rare, so the exit ladder never fires."""

    report = screen(
        make_snapshot(fdv_usd=2_000_000_000.0, liquidity_usd=40_000_000.0),
        settings, safe_authority(),
    )
    assert not report.passed
    assert any("above cap" in r for r in report.failures)


def test_overhang_is_bucketed_for_the_dashboard(settings):
    from memecoin_bot.safety import categorize

    report = screen(
        make_snapshot(fdv_usd=30_000_000.0, liquidity_usd=40_000.0),
        settings, safe_authority(),
    )
    overhang = [r for r in report.failures if "overhang" in r]
    assert categorize(overhang[0]) == "overhang"


def test_the_overhang_check_can_be_disabled():
    from memecoin_bot.config import Settings

    off = Settings(max_fdv_to_liquidity=0.0)
    report = screen(
        make_snapshot(fdv_usd=30_000_000.0, liquidity_usd=40_000.0),
        off, safe_authority(),
    )
    assert report.passed, report.failures
