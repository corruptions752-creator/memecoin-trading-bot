"""Tests for entry scoring and the exit ladder."""

from conftest import NOW, deterministic_settings, make_snapshot

from memecoin_bot.config import Settings
from memecoin_bot.models import ExitReason, Position
from memecoin_bot.strategy import decide_entry, decide_exit, score_entry


def make_position(**overrides) -> Position:
    defaults = dict(
        mint="MintAddress1111111111111111111111111111111",
        symbol="TEST",
        entry_price_usd=0.001,
        quantity=10_000.0,
        cost_usd=10.0,
        opened_at=NOW,
        entry_liquidity_usd=100_000.0,
    )
    defaults.update(overrides)
    return Position(**defaults)


# --- Entry ---------------------------------------------------------------

def test_flat_token_is_not_bought(settings):
    score, notes = score_entry(make_snapshot(price_change_5m=0.001), settings)
    assert score == 0.0
    assert "below entry threshold" in notes[0]


def test_vertical_candle_is_refused(settings):
    """Chasing a spike is how you become someone else's exit liquidity."""

    score, notes = score_entry(make_snapshot(price_change_5m=1.50), settings)
    assert score == 0.0
    assert "refusing to chase" in notes[0]


def test_healthy_momentum_scores(settings):
    score, _ = score_entry(make_snapshot(), settings)
    assert 0.0 < score <= 1.0


def test_collapsing_hourly_trend_halves_the_score(settings):
    healthy, _ = score_entry(make_snapshot(price_change_1h=0.10), settings)
    dying, notes = score_entry(make_snapshot(price_change_1h=-0.50), settings)
    assert dying < healthy
    assert any("1h trend" in note for note in notes)


def test_deeper_liquidity_scores_higher(settings):
    """Depth must help on its own, so hold turnover constant while it varies."""

    thin, _ = score_entry(
        make_snapshot(liquidity_usd=30_000.0, volume_24h_usd=150_000.0),
        settings,
    )
    deep, _ = score_entry(
        make_snapshot(liquidity_usd=2_000_000.0, volume_24h_usd=10_000_000.0),
        settings,
    )
    assert deep > thin


def test_turnover_scores_higher_at_equal_depth(settings):
    quiet, _ = score_entry(
        make_snapshot(liquidity_usd=100_000.0, volume_24h_usd=60_000.0),
        settings,
    )
    busy, _ = score_entry(
        make_snapshot(liquidity_usd=100_000.0, volume_24h_usd=1_500_000.0),
        settings,
    )
    assert busy > quiet


def test_buy_pressure_scores_higher(settings):
    weak, _ = score_entry(make_snapshot(buys_5m=85, sells_5m=100), settings)
    strong, _ = score_entry(make_snapshot(buys_5m=300, sells_5m=100), settings)
    assert strong > weak


def test_decide_entry_respects_the_threshold(settings):
    strict = Settings(min_entry_score=0.99)
    assert decide_entry(make_snapshot(), strict) is None

    loose = Settings(min_entry_score=0.01)
    decision = decide_entry(make_snapshot(), loose)
    assert decision is not None
    assert decision.score > 0


# --- Exit ----------------------------------------------------------------

def test_stop_loss_fires_at_the_configured_level(settings):
    position = make_position()
    snapshot = make_snapshot(price_usd=0.00084)  # -16%
    decision = decide_exit(position, snapshot, settings, NOW + 60)
    assert decision is not None
    assert decision.reason is ExitReason.STOP_LOSS
    assert decision.is_full_exit


def test_no_exit_inside_the_stop(settings):
    position = make_position()
    snapshot = make_snapshot(price_usd=0.00095)  # -5%
    assert decide_exit(position, snapshot, settings, NOW + 60) is None


def test_first_target_sells_half_not_all(settings):
    """Principal comes off the table; the runner stays on."""

    position = make_position()
    snapshot = make_snapshot(price_usd=0.002)  # 2x
    decision = decide_exit(position, snapshot, settings, NOW + 60)
    assert decision is not None
    assert decision.reason is ExitReason.TAKE_PROFIT
    assert decision.fraction == 0.5
    assert not decision.is_full_exit


def test_target_does_not_fire_twice(settings):
    position = make_position(took_first_profit=True, peak_price_usd=0.002)
    snapshot = make_snapshot(price_usd=0.0021)
    decision = decide_exit(position, snapshot, settings, NOW + 60)
    assert decision is None


def test_trailing_stop_protects_the_runner(settings):
    position = make_position(took_first_profit=True, peak_price_usd=0.004)
    snapshot = make_snapshot(price_usd=0.0029)  # -27.5% from peak
    decision = decide_exit(position, snapshot, settings, NOW + 60)
    assert decision is not None
    assert decision.reason is ExitReason.TRAILING_STOP
    assert decision.is_full_exit


def test_trailing_stop_allows_a_normal_pullback(settings):
    position = make_position(took_first_profit=True, peak_price_usd=0.004)
    snapshot = make_snapshot(price_usd=0.0035)  # -12.5% from peak
    assert decide_exit(position, snapshot, settings, NOW + 60) is None


def test_peak_updates_as_price_rises(settings):
    position = make_position(took_first_profit=True, peak_price_usd=0.002)
    decide_exit(position, make_snapshot(price_usd=0.005), settings, NOW + 60)
    assert position.peak_price_usd == 0.005


def test_time_stop_closes_a_stagnant_position(settings):
    position = make_position()
    snapshot = make_snapshot(price_usd=0.00101)
    decision = decide_exit(position, snapshot, settings, NOW + 7 * 3_600)
    assert decision is not None
    assert decision.reason is ExitReason.TIME_STOP


def test_liquidity_collapse_beats_profit_taking(settings):
    """A pool draining while you are up is a rug, not a win. Leave entirely."""

    position = make_position()
    snapshot = make_snapshot(price_usd=0.002, liquidity_usd=20_000.0)
    decision = decide_exit(position, snapshot, settings, NOW + 60)
    assert decision is not None
    assert decision.reason is ExitReason.LIQUIDITY_COLLAPSE
    assert decision.is_full_exit


def test_stop_loss_takes_priority_over_the_time_stop(settings):
    position = make_position()
    snapshot = make_snapshot(price_usd=0.0005)
    decision = decide_exit(position, snapshot, settings, NOW + 99 * 3_600)
    assert decision.reason is ExitReason.STOP_LOSS


def test_dead_price_feed_forces_an_exit(settings):
    position = make_position()
    decision = decide_exit(
        position, make_snapshot(price_usd=0.0), settings, NOW + 60
    )
    assert decision is not None
    assert decision.reason is ExitReason.LIQUIDITY_COLLAPSE


def test_full_ladder_sequence(settings):
    """Walk one winner through target, run-up, and trailing exit."""

    position = make_position()

    assert decide_exit(position, make_snapshot(price_usd=0.0015), settings, NOW) is None

    take = decide_exit(position, make_snapshot(price_usd=0.002), settings, NOW)
    assert take.reason is ExitReason.TAKE_PROFIT
    position.took_first_profit = True
    position.quantity *= 0.5

    assert decide_exit(position, make_snapshot(price_usd=0.006), settings, NOW) is None
    assert position.peak_price_usd == 0.006

    final = decide_exit(position, make_snapshot(price_usd=0.004), settings, NOW)
    assert final.reason is ExitReason.TRAILING_STOP


# --- Bad ticks from the pair feed ----------------------------------------

def test_a_tick_pricing_the_position_above_its_pool_is_ignored():
    """The pair feed does return these. It quoted one mint at 157,000x its
    real price, which cleared the profit target on every cycle and sent the
    engine into a sell that could never fill -- 105 times in five hours."""

    settings = deterministic_settings()
    position = Position(
        mint="Mint1", symbol="GLITCH", entry_price_usd=0.0015,
        quantity=32_000.0, cost_usd=50.0, opened_at=NOW,
        entry_liquidity_usd=205_000.0, peak_price_usd=0.0015,
    )
    bad = make_snapshot(price_usd=243.0, liquidity_usd=205_000.0)

    assert decide_exit(position, bad, settings, NOW + 60) is None


def test_a_bad_tick_does_not_set_the_high_water_mark():
    """A poisoned peak is permanent: the peak only ever ratchets up, so one
    bad tick disables the trailing stop for the life of the position."""

    settings = deterministic_settings()
    position = Position(
        mint="Mint1", symbol="GLITCH", entry_price_usd=0.0015,
        quantity=32_000.0, cost_usd=50.0, opened_at=NOW,
        entry_liquidity_usd=205_000.0, peak_price_usd=0.0015,
    )
    decide_exit(position, make_snapshot(price_usd=243.0,
                                        liquidity_usd=205_000.0), settings, NOW + 60)
    assert position.peak_price_usd == 0.0015


def test_a_real_gain_inside_the_pool_is_still_acted_on():
    """The guard must not swallow a genuine winner. A position may be a
    large share of its pool -- just never a multiple of it."""

    settings = deterministic_settings(take_profit_multiple=3.0)
    position = Position(
        mint="Mint1", symbol="RUNNER", entry_price_usd=0.001,
        quantity=50_000.0, cost_usd=50.0, opened_at=NOW,
        entry_liquidity_usd=200_000.0, peak_price_usd=0.001,
    )
    # 50,000 units at $0.004 is $200 against a $200,000 pool: plainly real.
    good = make_snapshot(price_usd=0.004, liquidity_usd=200_000.0)

    decision = decide_exit(position, good, settings, NOW + 60)
    assert decision is not None
    assert decision.reason is ExitReason.TAKE_PROFIT
    assert position.peak_price_usd == 0.004


def test_a_pool_with_no_liquidity_falls_through_to_the_other_rules():
    """With nothing to compare against the guard must not swallow the
    liquidity-collapse exit, which is the rule that matters there."""

    settings = deterministic_settings()
    position = Position(
        mint="Mint1", symbol="DRAINED", entry_price_usd=0.001,
        quantity=50_000.0, cost_usd=50.0, opened_at=NOW,
        entry_liquidity_usd=200_000.0, peak_price_usd=0.001,
    )
    decision = decide_exit(
        position, make_snapshot(price_usd=0.001, liquidity_usd=0.0),
        settings, NOW + 60,
    )
    assert decision is not None
    assert decision.reason is ExitReason.LIQUIDITY_COLLAPSE


def test_a_poisoned_peak_repairs_itself_on_the_next_good_tick():
    """The high-water mark only ratchets up, so a peak set by a bad tick is
    permanent and the trailing stop can never fire again. One live position
    carried a peak 157,000x its entry."""

    settings = deterministic_settings()
    position = Position(
        mint="Mint1", symbol="POISONED", entry_price_usd=0.0015,
        quantity=32_000.0, cost_usd=50.0, opened_at=NOW,
        entry_liquidity_usd=205_000.0,
        peak_price_usd=243.043,          # 32,000 units x $243 = $7.8m
    )
    good = make_snapshot(price_usd=0.0016, liquidity_usd=205_000.0)

    decide_exit(position, good, settings, NOW + 60)
    assert position.peak_price_usd == 0.0016
    assert position.value_usd(position.peak_price_usd) < good.liquidity_usd


def test_a_reachable_peak_is_left_alone():
    settings = deterministic_settings()
    position = Position(
        mint="Mint1", symbol="RUNNER", entry_price_usd=0.001,
        quantity=50_000.0, cost_usd=50.0, opened_at=NOW,
        entry_liquidity_usd=200_000.0, peak_price_usd=0.003,
    )
    decide_exit(position, make_snapshot(price_usd=0.0025,
                                        liquidity_usd=200_000.0), settings, NOW + 60)
    assert position.peak_price_usd == 0.003
