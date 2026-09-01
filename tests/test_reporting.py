"""Tests for performance analytics."""

from memecoin_bot.models import ExitReason, Position
from memecoin_bot.reporting import max_drawdown, sharpe_ratio, summarize
from memecoin_bot.store import Store

NOW = 1_700_000_000.0


def make_position(realized, **overrides):
    defaults = dict(
        mint="M", symbol="T", entry_price_usd=0.001, quantity=1_000.0,
        cost_usd=10.0, opened_at=NOW, entry_liquidity_usd=100_000.0,
    )
    defaults.update(overrides)
    position = Position(**defaults)
    position.realized_usd = realized
    return position


def store_with(results, reasons=None):
    """A store holding closed positions with the given realized results."""

    store = Store(":memory:")
    reasons = reasons or [ExitReason.STOP_LOSS] * len(results)
    for value, reason in zip(results, reasons):
        position = store.open_position(make_position(0.0))
        position.realized_usd = value
        store.close_position(position, reason, NOW + 60)
    return store


# --- Drawdown ------------------------------------------------------------

def test_drawdown_cannot_exceed_total_loss():
    """Regression: a curve anchored at zero reported -183%, which is impossible."""

    store = store_with([-10.0, -10.0, -10.0, -10.0])
    performance = summarize(store, starting_bankroll_usd=1_000.0)
    assert -1.0 <= performance.max_drawdown <= 0.0


def test_drawdown_is_measured_against_starting_capital():
    store = store_with([-50.0])
    performance = summarize(store, starting_bankroll_usd=1_000.0)
    assert abs(performance.max_drawdown - (-0.05)) < 1e-9


def test_a_bounded_curve_is_returned_without_a_bankroll():
    """No anchor supplied must still produce a sane number, not a blowup."""

    store = store_with([-10.0, -10.0])
    performance = summarize(store)
    assert -1.0 <= performance.max_drawdown <= 0.0


def test_drawdown_of_a_rising_curve_is_zero():
    assert max_drawdown([100.0, 110.0, 120.0]) == 0.0


def test_drawdown_finds_the_deepest_trough():
    assert abs(max_drawdown([100.0, 200.0, 100.0, 150.0]) - (-0.5)) < 1e-9


def test_drawdown_of_an_empty_curve_is_zero():
    assert max_drawdown([]) == 0.0


# --- Sharpe --------------------------------------------------------------

def test_sharpe_is_positive_for_consistent_gains():
    assert sharpe_ratio([1.0, 1.1, 0.9, 1.05]) > 0


def test_sharpe_is_negative_for_consistent_losses():
    assert sharpe_ratio([-1.0, -1.1, -0.9]) < 0


def test_sharpe_needs_at_least_two_points():
    assert sharpe_ratio([1.0]) == 0.0
    assert sharpe_ratio([]) == 0.0


def test_zero_variance_does_not_divide_by_zero():
    assert sharpe_ratio([1.0, 1.0, 1.0]) == 0.0


# --- Expectancy and streaks ---------------------------------------------

def test_expectancy_is_dollars_per_trade():
    store = store_with([10.0, -5.0, 10.0, -5.0])
    assert abs(summarize(store, 1_000.0).expectancy_usd - 2.5) < 1e-9


def test_a_losing_strategy_reports_negative_expectancy():
    store = store_with([-5.0, -5.0, 2.0])
    assert summarize(store, 1_000.0).expectancy_usd < 0


def test_the_worst_losing_streak_is_counted():
    store = store_with([-1.0, -1.0, -1.0, 5.0, -1.0])
    assert summarize(store, 1_000.0).largest_loss_streak == 3


def test_pnl_is_broken_down_by_exit_reason():
    store = store_with(
        [-10.0, -10.0, 30.0],
        [ExitReason.STOP_LOSS, ExitReason.STOP_LOSS, ExitReason.TRAILING_STOP],
    )
    performance = summarize(store, 1_000.0)
    assert abs(performance.pnl_by_exit["stop_loss"] - (-20.0)) < 1e-9
    assert abs(performance.pnl_by_exit["trailing_stop"] - 30.0) < 1e-9


def test_the_report_renders_every_metric():
    store = store_with([10.0, -5.0])
    rendered = summarize(store, 1_000.0).render()
    for label in ("Expectancy", "Max drawdown", "Sharpe", "Worst streak"):
        assert label in rendered


def test_an_empty_store_reports_no_trades():
    assert "No closed trades yet." in summarize(Store(":memory:"), 1_000.0).render()


def test_results_are_split_per_playbook(tmp_path):
    """Three theses averaged into one number cannot tell you which to keep."""

    from memecoin_bot.models import ExitReason, Position
    from memecoin_bot.reporting import summarize
    from memecoin_bot.store import Store

    store = Store(str(tmp_path / "t.sqlite3"))
    for name, pnl in (("momentum", 40.0), ("momentum", -10.0), ("reversal", -25.0)):
        position = store.open_position(Position(
            mint="M" * 32, symbol="T", entry_price_usd=1.0, quantity=1.0,
            cost_usd=50.0, opened_at=0.0, entry_liquidity_usd=10_000.0,
            strategy=name,
        ))
        position.realized_usd = pnl
        store.close_position(position, ExitReason.STOP_LOSS, 1.0)

    performance = summarize(store, 1_000.0)

    assert performance.by_strategy["momentum"]["trades"] == 2
    assert performance.by_strategy["momentum"]["pnl"] == 30.0
    assert performance.by_strategy["momentum"]["wins"] == 1
    assert performance.by_strategy["reversal"]["pnl"] == -25.0
    assert "By playbook" in performance.render()
