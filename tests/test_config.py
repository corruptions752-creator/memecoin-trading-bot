"""Tests for configuration loading and the live-mode guard."""

import pytest

from memecoin_bot.config import LIVE, PAPER, load_settings


def test_defaults_to_paper_mode(monkeypatch):
    monkeypatch.delenv("MEMEBOT_MODE", raising=False)
    assert load_settings().mode == PAPER


def test_live_mode_requires_explicit_acknowledgement(monkeypatch):
    """Live trading must never be one environment variable away."""

    monkeypatch.setenv("MEMEBOT_MODE", "live")
    monkeypatch.delenv("MEMEBOT_I_UNDERSTAND_THE_RISK", raising=False)
    with pytest.raises(RuntimeError, match="Refusing to start in live mode"):
        load_settings()


def test_live_mode_starts_once_acknowledged(monkeypatch):
    monkeypatch.setenv("MEMEBOT_MODE", "live")
    monkeypatch.setenv("MEMEBOT_I_UNDERSTAND_THE_RISK", "yes")
    assert load_settings().mode == LIVE


def test_unknown_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("MEMEBOT_MODE", "yolo")
    with pytest.raises(RuntimeError, match="must be"):
        load_settings()


def test_risk_fraction_is_capped(monkeypatch):
    """No configuration may bet a quarter of the bankroll on one trade."""

    monkeypatch.setenv("MEMEBOT_RISK_FRACTION", "0.9")
    with pytest.raises(RuntimeError, match="at most"):
        load_settings()


def test_stop_loss_cannot_be_disabled(monkeypatch):
    monkeypatch.setenv("MEMEBOT_STOP_LOSS_PCT", "0")
    with pytest.raises(RuntimeError, match="at least"):
        load_settings()


def test_non_numeric_values_are_rejected(monkeypatch):
    monkeypatch.setenv("MEMEBOT_BANKROLL_USD", "lots")
    with pytest.raises(RuntimeError, match="must be a number"):
        load_settings()


def test_environment_overrides_are_applied(monkeypatch):
    monkeypatch.setenv("MEMEBOT_BANKROLL_USD", "500")
    monkeypatch.setenv("MEMEBOT_MAX_POSITIONS", "5")
    monkeypatch.setenv("MEMEBOT_RISK_FRACTION", "0.02")
    settings = load_settings()
    assert settings.starting_bankroll_usd == 500.0
    assert settings.max_open_positions == 5
    assert settings.risk_fraction_per_trade == 0.02


# --- Risk profiles -------------------------------------------------------

def test_profiles_scale_size_and_selectivity_together(monkeypatch):
    """A posture is a coherent stance, not one dial."""

    from memecoin_bot.config import PROFILES

    sizes, scores = [], []
    for name in ("conservative", "balanced", "aggressive"):
        monkeypatch.setenv("MEMEBOT_PROFILE", name)
        s = load_settings()
        sizes.append(s.risk_fraction_per_trade)
        scores.append(s.min_entry_score)

    assert sizes == sorted(sizes), "size must rise with aggression"
    assert scores == sorted(scores, reverse=True), "selectivity must fall"


def test_the_default_profile_is_conservative(monkeypatch):
    monkeypatch.delenv("MEMEBOT_PROFILE", raising=False)
    assert load_settings().risk_fraction_per_trade == 0.01


def test_an_unknown_profile_is_rejected(monkeypatch):
    monkeypatch.setenv("MEMEBOT_PROFILE", "yolo")
    with pytest.raises(RuntimeError, match="MEMEBOT_PROFILE"):
        load_settings()


def test_an_explicit_variable_beats_the_profile(monkeypatch):
    """A profile is a starting point, not a cage."""

    monkeypatch.setenv("MEMEBOT_PROFILE", "aggressive")
    monkeypatch.setenv("MEMEBOT_RISK_FRACTION", "0.02")
    assert load_settings().risk_fraction_per_trade == 0.02


def test_the_hard_size_cap_survives_any_profile(monkeypatch):
    """No posture may bet a quarter of the bankroll on one trade."""

    monkeypatch.setenv("MEMEBOT_PROFILE", "aggressive")
    monkeypatch.setenv("MEMEBOT_RISK_FRACTION", "0.9")
    with pytest.raises(RuntimeError, match="at most"):
        load_settings()


# --- Profile coherence ---------------------------------------------------

def test_every_profile_is_internally_consistent(monkeypatch):
    """A posture that lowers one floor without the others opens a band it
    can never trade through.

    This was not hypothetical twice over: the LP substitute stayed at $100k
    against a $10k liquidity floor, and then the volume floor stayed at $50k
    against the same floor, rejecting 121 of 338 tokens on a live scan. Both
    were invisible until a scan was read closely.
    """

    from memecoin_bot.config import PROFILES

    for name in PROFILES:
        monkeypatch.setenv("MEMEBOT_PROFILE", name)
        s = load_settings()

        assert s.lp_substitute_min_liquidity_usd >= s.min_liquidity_usd, (
            f"{name}: LP substitute below the liquidity floor"
        )
        assert s.min_volume_24h_usd <= s.min_liquidity_usd * 3, (
            f"{name}: volume floor implausible for its liquidity floor"
        )
        assert s.min_momentum_5m_pct < s.max_momentum_5m_pct, (
            f"{name}: empty momentum band"
        )
        assert 0.0 < s.min_entry_score < 1.0, f"{name}: unreachable score"


def test_no_profile_can_overcommit_the_bankroll(monkeypatch):
    from memecoin_bot.config import PROFILES

    for name in PROFILES:
        monkeypatch.setenv("MEMEBOT_PROFILE", name)
        s = load_settings()
        committed = s.risk_fraction_per_trade * s.max_open_positions
        assert committed <= 1.0, f"{name}: {committed:.0%} of bankroll at risk"


def test_the_daily_limit_allows_at_least_one_full_loss(monkeypatch):
    """A breaker that trips before a single stop-out completes would halt
    trading on the first losing trade."""

    from memecoin_bot.config import PROFILES

    for name in PROFILES:
        monkeypatch.setenv("MEMEBOT_PROFILE", name)
        s = load_settings()
        one_stop = s.risk_fraction_per_trade * s.stop_loss_pct
        assert s.daily_loss_limit_pct > one_stop, name


def test_every_profile_defines_every_key(monkeypatch):
    """A key present in one profile and missing from another would silently
    fall back to a default that does not match the posture."""

    from memecoin_bot.config import PROFILES

    keysets = [set(v) for v in PROFILES.values()]
    assert all(k == keysets[0] for k in keysets), (
        f"profiles define different keys: "
        f"{[sorted(k ^ keysets[0]) for k in keysets]}"
    )


# --- Verification mode ---------------------------------------------------

def test_verification_defaults_to_strict(monkeypatch):
    monkeypatch.delenv("MEMEBOT_VERIFICATION", raising=False)
    assert load_settings().verification == "strict"


def test_advisory_verification_is_refused_in_live_mode(monkeypatch):
    """The contract checks are what stand between the bot and a honeypot."""

    monkeypatch.setenv("MEMEBOT_MODE", "live")
    monkeypatch.setenv("MEMEBOT_I_UNDERSTAND_THE_RISK", "yes")
    monkeypatch.setenv("MEMEBOT_VERIFICATION", "advisory")
    with pytest.raises(RuntimeError, match="Refusing to run live"):
        load_settings()


def test_advisory_verification_is_allowed_in_paper_mode(monkeypatch):
    monkeypatch.setenv("MEMEBOT_MODE", "paper")
    monkeypatch.setenv("MEMEBOT_VERIFICATION", "advisory")
    assert load_settings().verification == "advisory"


def test_an_unknown_verification_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("MEMEBOT_VERIFICATION", "loose")
    with pytest.raises(RuntimeError, match="MEMEBOT_VERIFICATION"):
        load_settings()
