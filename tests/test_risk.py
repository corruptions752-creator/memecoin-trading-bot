"""Tests for position sizing and the daily circuit breaker."""

from dataclasses import replace

from conftest import NOW

from memecoin_bot.config import Settings
from memecoin_bot.risk import RiskManager, utc_day


def test_position_size_is_one_percent_of_bankroll(settings):
    risk = RiskManager.start(settings, NOW)
    assert risk.position_size_usd() == 10.0


def test_position_size_tracks_whole_bankroll_not_free_cash(settings):
    """Holding positions must not shrink the size of the next one."""

    risk = RiskManager.start(settings, NOW)
    risk.record_buy(10.0)
    risk.record_buy(10.0)
    assert risk.bankroll_usd == 1_000.0
    assert risk.position_size_usd() == 10.0


def test_position_size_shrinks_after_losses(settings):
    risk = RiskManager.start(settings, NOW)
    risk.record_buy(100.0)
    risk.record_sell(50.0, 100.0, NOW)
    assert risk.bankroll_usd == 950.0
    assert risk.position_size_usd() == 9.5


def test_position_size_is_capped_by_available_cash(settings):
    risk = RiskManager.start(settings, NOW)
    risk.cash_usd = 3.0
    risk.open_cost_usd = 997.0
    assert risk.position_size_usd() == 3.0


def test_position_limit_blocks_extra_entries(settings):
    risk = RiskManager.start(settings, NOW)
    allowed, why = risk.can_open(3, NOW)
    assert not allowed
    assert "position limit" in why


def test_below_position_limit_is_allowed(settings):
    risk = RiskManager.start(settings, NOW)
    allowed, why = risk.can_open(2, NOW)
    assert allowed, why


def test_dust_bankroll_blocks_entry():
    settings = Settings(starting_bankroll_usd=100.0, min_position_usd=5.0)
    risk = RiskManager.start(settings, NOW)
    allowed, why = risk.can_open(0, NOW)
    assert not allowed
    assert "below minimum" in why


def test_daily_loss_limit_halts_trading(settings):
    """Five percent of a $1,000 bankroll is $50 of realized loss."""

    risk = RiskManager.start(settings, NOW)
    risk.record_buy(200.0)
    risk.record_sell(140.0, 200.0, NOW)

    assert risk.halted
    assert "daily loss limit" in risk.halt_reason
    allowed, why = risk.can_open(0, NOW)
    assert not allowed
    assert "halted" in why


def test_small_losses_do_not_halt(settings):
    risk = RiskManager.start(settings, NOW)
    risk.record_buy(100.0)
    risk.record_sell(90.0, 100.0, NOW)
    assert not risk.halted


def test_wins_offset_losses_within_the_day(settings):
    risk = RiskManager.start(settings, NOW)
    risk.record_buy(200.0)
    risk.record_sell(160.0, 200.0, NOW)   # -40
    risk.record_buy(100.0)
    risk.record_sell(180.0, 100.0, NOW)   # +80
    assert not risk.halted
    assert risk.realized_today_usd == 40.0


def test_new_utc_day_clears_a_loss_halt(settings):
    risk = RiskManager.start(settings, NOW)
    risk.record_buy(200.0)
    risk.record_sell(140.0, 200.0, NOW)
    assert risk.halted

    tomorrow = NOW + 86_400
    assert risk.roll_day(tomorrow)
    assert not risk.halted
    assert risk.realized_today_usd == 0.0


def test_new_day_does_not_clear_a_manual_halt(settings):
    """A halt a human set stays set until a human clears it."""

    risk = RiskManager.start(settings, NOW)
    risk.halt("operator stopped trading")
    risk.roll_day(NOW + 86_400)
    assert risk.halted
    assert risk.halt_reason == "operator stopped trading"


def test_resume_clears_a_halt(settings):
    risk = RiskManager.start(settings, NOW)
    risk.halt("whatever")
    risk.resume()
    assert not risk.halted
    allowed, _ = risk.can_open(0, NOW)
    assert allowed


def test_roll_day_is_idempotent_within_a_day(settings):
    risk = RiskManager.start(settings, NOW)
    assert not risk.roll_day(NOW + 60)


def test_cash_conservation_across_a_round_trip(settings):
    risk = RiskManager.start(settings, NOW)
    start = risk.bankroll_usd
    risk.record_buy(50.0)
    assert risk.cash_usd == start - 50.0
    risk.record_sell(75.0, 50.0, NOW)
    assert risk.cash_usd == start + 25.0
    assert risk.open_cost_usd == 0.0


def test_utc_day_formatting():
    assert utc_day(NOW) == "2023-11-14"


def test_aggressive_config_has_a_wider_limit():
    settings = Settings(daily_loss_limit_pct=0.20)
    risk = RiskManager.start(settings, NOW)
    risk.record_buy(300.0)
    risk.record_sell(200.0, 300.0, NOW)  # -100, under the $200 limit
    assert not risk.halted
