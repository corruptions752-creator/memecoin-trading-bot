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
