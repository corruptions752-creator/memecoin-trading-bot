"""End-to-end tests of the trading loop against a scripted market."""

from conftest import FakeMarket, NOW, make_snapshot, safe_authority

from memecoin_bot.broker import PaperBroker
from memecoin_bot.config import Settings
from memecoin_bot.engine import TradingEngine
from memecoin_bot.models import ExitReason
from memecoin_bot.risk import RiskManager
from memecoin_bot.safety import TokenAuthority
from memecoin_bot.store import Store


class FixedAuthority:
    """Authority provider returning the same verdict for every mint."""

    def __init__(self, authority: TokenAuthority) -> None:
        self.authority = authority

    def fetch(self, mint: str) -> TokenAuthority:
        return self.authority


def build_engine(candidates=None, settings=None, authority=None, **kwargs):
    """Assemble an engine wired to fakes."""

    settings = settings or Settings(min_entry_score=0.01)
    candidates = list(candidates or [])
    market = FakeMarket(
        candidates=candidates,
        prices={snapshot.mint: snapshot for snapshot in candidates},
    )
    store = Store(":memory:")
    engine = TradingEngine(
        settings,
        market,
        PaperBroker(settings),
        RiskManager.start(settings, NOW),
        store,
        FixedAuthority(authority or safe_authority()),
        **kwargs,
    )
    return engine, market, store


def test_a_clean_candidate_is_bought():
    engine, _, store = build_engine([make_snapshot(symbol="GOOD")])
    report = engine.run_cycle(NOW)

    assert report.entered == ["GOOD"]
    assert len(engine.positions) == 1
    assert len(store.load_open_positions()) == 1


def test_capital_moves_from_cash_into_the_position():
    engine, _, _ = build_engine([make_snapshot()])
    engine.run_cycle(NOW)

    assert engine.risk.cash_usd < 1_000.0
    assert engine.risk.open_cost_usd > 0
    # One percent of bankroll, plus costs.
    assert 9.0 < engine.positions[0].cost_usd < 11.0


def test_a_rugged_token_is_never_bought():
    engine, _, _ = build_engine(
        [make_snapshot()], authority=safe_authority(sell_simulation_ok=False)
    )
    report = engine.run_cycle(NOW)

    assert report.entered == []
    assert report.rejected == 1
    assert engine.positions == []


def test_position_limit_is_respected():
    candidates = [
        make_snapshot(mint=f"Mint{i}", symbol=f"T{i}") for i in range(10)
    ]
    engine, _, _ = build_engine(candidates)
    engine.run_cycle(NOW)

    assert len(engine.positions) == 3


def test_the_highest_scoring_candidate_is_taken_first():
    weak = make_snapshot(
        mint="MintWeak", symbol="WEAK", price_change_5m=0.03,
        buys_5m=52, sells_5m=50, volume_24h_usd=60_000.0,
    )
    strong = make_snapshot(
        mint="MintStrong", symbol="STRONG", price_change_5m=0.18,
        buys_5m=400, sells_5m=100, volume_24h_usd=900_000.0,
    )
    settings = Settings(min_entry_score=0.01, max_open_positions=1)
    engine, _, _ = build_engine([weak, strong], settings=settings)
    engine.run_cycle(NOW)

    assert [p.symbol for p in engine.positions] == ["STRONG"]


def test_an_existing_holding_is_not_bought_again():
    snapshot = make_snapshot()
    engine, _, _ = build_engine([snapshot])
    engine.run_cycle(NOW)
    engine.run_cycle(NOW + 60)

    assert len(engine.positions) == 1


def test_stop_loss_closes_the_position_and_returns_cash():
    snapshot = make_snapshot(symbol="DUMP")
    engine, market, store = build_engine([snapshot])
    engine.run_cycle(NOW)
    entry_cash = engine.risk.cash_usd

    market.set_price(snapshot.mint, 0.0005)  # -50%
    report = engine.run_cycle(NOW + 300)

    assert (("DUMP", ExitReason.STOP_LOSS)) in report.exited
    assert engine.positions == []
    assert engine.risk.cash_usd > entry_cash      # proceeds returned
    assert engine.risk.bankroll_usd < 1_000.0     # but at a loss
    assert store.closed_positions()[0]["close_reason"] == "stop_loss"


def test_take_profit_banks_half_and_keeps_a_runner():
    snapshot = make_snapshot(symbol="MOON")
    engine, market, store = build_engine([snapshot])
    engine.run_cycle(NOW)
    position = engine.positions[0]
    original_quantity = position.quantity

    market.set_price(snapshot.mint, position.entry_price_usd * 2.2)
    report = engine.run_cycle(NOW + 300)

    assert (("MOON", ExitReason.TAKE_PROFIT)) in report.exited
    assert len(engine.positions) == 1                       # runner intact
    assert position.quantity == original_quantity * 0.5
    assert position.took_first_profit
    assert store.load_open_positions()[0].took_first_profit


def test_the_full_winning_ladder_realizes_a_profit():
    snapshot = make_snapshot(symbol="RUN")
    engine, market, store = build_engine([snapshot])
    engine.run_cycle(NOW)
    entry = engine.positions[0].entry_price_usd

    market.set_price(snapshot.mint, entry * 2.5)     # first target
    engine.run_cycle(NOW + 300)
    market.set_price(snapshot.mint, entry * 6.0)     # runner extends
    engine.run_cycle(NOW + 600)
    market.set_price(snapshot.mint, entry * 4.0)     # -33% from peak
    report = engine.run_cycle(NOW + 900)

    assert (("RUN", ExitReason.TRAILING_STOP)) in report.exited
    assert engine.positions == []
    assert engine.risk.bankroll_usd > 1_000.0
    assert store.closed_positions()[0]["realized_usd"] > 0


def test_liquidity_collapse_forces_an_exit_even_while_up():
    snapshot = make_snapshot(symbol="RUG")
    engine, market, _ = build_engine([snapshot])
    engine.run_cycle(NOW)

    market.set_price(snapshot.mint, 0.0015, liquidity_usd=5_000.0)
    report = engine.run_cycle(NOW + 300)

    assert (("RUG", ExitReason.LIQUIDITY_COLLAPSE)) in report.exited
    assert engine.positions == []


def test_time_stop_closes_a_stagnant_position():
    snapshot = make_snapshot(symbol="FLAT")
    engine, market, _ = build_engine([snapshot])
    engine.run_cycle(NOW)

    market.set_price(snapshot.mint, snapshot.price_usd * 1.01)
    report = engine.run_cycle(NOW + 7 * 3_600)

    assert (("FLAT", ExitReason.TIME_STOP)) in report.exited


def test_a_missing_quote_holds_rather_than_dumping_blind():
    snapshot = make_snapshot(symbol="QUIET")
    engine, market, _ = build_engine([snapshot])
    engine.run_cycle(NOW)

    market.prices.pop(snapshot.mint)
    engine.run_cycle(NOW + 300)

    assert len(engine.positions) == 1


def test_a_data_outage_does_not_open_positions():
    engine, market, _ = build_engine([make_snapshot()])
    market.candidates = []
    report = engine.run_cycle(NOW)

    assert report.scanned == 0
    assert report.entered == []


def test_the_daily_loss_limit_stops_new_entries():
    settings = Settings(
        min_entry_score=0.01, max_open_positions=1, risk_fraction_per_trade=0.20
    )
    snapshot = make_snapshot(symbol="LOSS")
    engine, market, _ = build_engine([snapshot], settings=settings)

    engine.run_cycle(NOW)
    assert len(engine.positions) == 1

    market.set_price(snapshot.mint, 0.0001)  # -90%
    engine.run_cycle(NOW + 300)
    assert engine.risk.halted

    market.set_price(snapshot.mint, 0.001)
    market.candidates = [make_snapshot(mint="MintNew", symbol="NEW")]
    report = engine.run_cycle(NOW + 600)

    assert report.entered == []
    assert "halted" in report.skipped_reason


def test_positions_are_resumed_after_a_restart(tmp_path):
    path = str(tmp_path / "trading.sqlite3")
    settings = Settings(min_entry_score=0.01, database_path=path)
    snapshot = make_snapshot(symbol="KEEP")
    market = FakeMarket([snapshot], {snapshot.mint: snapshot})

    store = Store(path)
    first = TradingEngine(
        settings, market, PaperBroker(settings),
        RiskManager.start(settings, NOW), store, FixedAuthority(safe_authority()),
    )
    first.run_cycle(NOW)
    assert len(first.positions) == 1
    store.close()

    revived = Store(path)
    second = TradingEngine(
        settings, market, PaperBroker(settings),
        RiskManager.start(settings, NOW), revived,
        FixedAuthority(safe_authority()),
    )
    assert len(second.positions) == 1
    assert second.positions[0].symbol == "KEEP"


def test_contract_checks_can_be_relaxed_for_paper_only():
    engine, _, _ = build_engine(
        [make_snapshot()],
        authority=TokenAuthority(),      # knows nothing
        enforce_contract_checks=False,
    )
    report = engine.run_cycle(NOW)
    assert report.entered  # market screen alone let it through


def test_contract_checks_are_enforced_by_default():
    engine, _, _ = build_engine([make_snapshot()], authority=TokenAuthority())
    report = engine.run_cycle(NOW)
    assert report.entered == []


def test_close_all_flattens_everything():
    candidates = [
        make_snapshot(mint=f"Mint{i}", symbol=f"T{i}") for i in range(3)
    ]
    engine, _, store = build_engine(candidates)
    engine.run_cycle(NOW)
    assert len(engine.positions) == 3

    assert engine.close_all() == 3
    assert engine.positions == []
    assert len(store.closed_positions()) == 3


def test_equity_reflects_a_price_move():
    snapshot = make_snapshot()
    engine, market, _ = build_engine([snapshot])
    engine.run_cycle(NOW)

    market.set_price(snapshot.mint, snapshot.price_usd * 3)
    assert engine.equity_usd() > 1_000.0


def test_a_cycle_never_raises_on_a_broken_snapshot():
    engine, market, _ = build_engine([make_snapshot(symbol="ODD")])
    engine.run_cycle(NOW)
    market.set_price(market.candidates[0].mint, 0.0)
    engine.run_cycle(NOW + 60)  # must not raise
    assert engine.positions == []


# --- Re-entry control ----------------------------------------------------

def test_a_stopped_out_token_is_not_rebought_in_the_same_cycle():
    """Regression: selling at the stop and instantly re-buying is a loss loop."""

    snapshot = make_snapshot(symbol="DUMP")
    engine, market, store = build_engine([snapshot])
    engine.run_cycle(NOW)
    assert len(engine.positions) == 1

    # Price craters, then recovers to a level that still scores well.
    market.set_price(snapshot.mint, 0.0005)
    engine.run_cycle(NOW + 300)
    assert engine.positions == []

    market.set_price(snapshot.mint, 0.001)
    market.candidates = [make_snapshot(mint=snapshot.mint, symbol="DUMP")]
    report = engine.run_cycle(NOW + 600)

    assert report.entered == []
    assert engine.positions == []
    assert store.is_blocked(snapshot.mint, NOW + 600)


def test_the_cooldown_expires():
    snapshot = make_snapshot(symbol="LATER")
    engine, market, _ = build_engine([snapshot])
    engine.run_cycle(NOW)
    market.set_price(snapshot.mint, 0.0005)
    engine.run_cycle(NOW + 300)

    market.set_price(snapshot.mint, 0.001)
    market.candidates = [make_snapshot(mint=snapshot.mint, symbol="LATER")]

    after = NOW + 300 + 6 * 3_600 + 1
    report = engine.run_cycle(after)
    assert report.entered == ["LATER"]


def test_a_rug_is_blocked_permanently():
    """A pool that drained under a position never gets a second chance."""

    snapshot = make_snapshot(symbol="RUG")
    engine, market, store = build_engine([snapshot])
    engine.run_cycle(NOW)

    market.set_price(snapshot.mint, 0.0015, liquidity_usd=5_000.0)
    engine.run_cycle(NOW + 300)
    assert engine.positions == []

    market.candidates = [make_snapshot(mint=snapshot.mint, symbol="RUG")]
    far_future = NOW + 365 * 86_400
    report = engine.run_cycle(far_future)

    assert report.entered == []
    assert store.is_blocked(snapshot.mint, far_future)


def test_a_rug_ban_is_not_downgraded_by_a_later_timed_block():
    store = Store(":memory:")
    store.block_mint(
        "Mint111", NOW, "liquidity collapse", permanent=True, at=NOW
    )
    store.block_mint("Mint111", NOW + 60, "cooldown after stop_loss", at=NOW)
    assert store.is_blocked("Mint111", NOW + 10 * 365 * 86_400)


def test_a_cooldown_is_extended_never_shortened():
    store = Store(":memory:")
    store.block_mint("Mint111", NOW + 10_000, "long", at=NOW)
    store.block_mint("Mint111", NOW + 10, "short", at=NOW)
    assert store.is_blocked("Mint111", NOW + 5_000)


def test_blocks_survive_a_restart(tmp_path):
    path = str(tmp_path / "trading.sqlite3")
    store = Store(path)
    store.block_mint("MintRug", NOW, "rug", permanent=True, at=NOW)
    store.close()

    assert Store(path).is_blocked("MintRug", NOW + 86_400)


def test_take_profit_does_not_block_the_remaining_runner():
    """A partial exit must not bar the position that is still open."""

    snapshot = make_snapshot(symbol="MOON")
    engine, market, store = build_engine([snapshot])
    engine.run_cycle(NOW)
    position = engine.positions[0]

    market.set_price(snapshot.mint, position.entry_price_usd * 2.2)
    engine.run_cycle(NOW + 300)

    assert len(engine.positions) == 1
    assert not store.is_blocked(snapshot.mint, NOW + 300)


def test_close_all_also_blocks_reentry():
    snapshot = make_snapshot(symbol="FLAT")
    engine, market, store = build_engine([snapshot])
    engine.run_cycle(NOW)
    engine.close_all()
    assert store.is_blocked(snapshot.mint, NOW + 60)


def test_cooldown_can_be_disabled_by_configuration():
    settings = Settings(min_entry_score=0.01, reentry_cooldown_seconds=0)
    snapshot = make_snapshot(symbol="FREE")
    engine, market, store = build_engine([snapshot], settings=settings)
    engine.run_cycle(NOW)
    market.set_price(snapshot.mint, 0.0005)
    engine.run_cycle(NOW + 300)
    assert not store.is_blocked(snapshot.mint, NOW + 300)
