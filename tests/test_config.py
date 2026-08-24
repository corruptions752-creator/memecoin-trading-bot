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
