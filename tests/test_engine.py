"""End-to-end tests of the trading loop against a scripted market."""

from conftest import (
    FakeMarket, NOW, deterministic_settings, make_snapshot, safe_authority,
)

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

    settings = settings or deterministic_settings(min_entry_score=0.01)
    candidates = list(candidates or [])
    market = FakeMarket(
        candidates=candidates,
        prices={snapshot.mint: snapshot for snapshot in candidates},
    )
    store = Store(":memory:")
    engine = TradingEngine(
        settings,
        market,
        PaperBroker(settings, seed=1),
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
    settings = deterministic_settings(min_entry_score=0.01, max_open_positions=1)
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
    settings = deterministic_settings(
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
    settings = deterministic_settings(min_entry_score=0.01, database_path=path)
    snapshot = make_snapshot(symbol="KEEP")
    market = FakeMarket([snapshot], {snapshot.mint: snapshot})

    store = Store(path)
    first = TradingEngine(
        settings, market, PaperBroker(settings, seed=1),
        RiskManager.start(settings, NOW), store, FixedAuthority(safe_authority()),
    )
    first.run_cycle(NOW)
    assert len(first.positions) == 1
    store.close()

    revived = Store(path)
    second = TradingEngine(
        settings, market, PaperBroker(settings, seed=1),
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
    settings = deterministic_settings(
        min_entry_score=0.01, reentry_cooldown_seconds=0
    )
    snapshot = make_snapshot(symbol="FREE")
    engine, market, store = build_engine([snapshot], settings=settings)
    engine.run_cycle(NOW)
    market.set_price(snapshot.mint, 0.0005)
    engine.run_cycle(NOW + 300)
    assert not store.is_blocked(snapshot.mint, NOW + 300)


# --- Screening order -----------------------------------------------------

class CountingAuthority:
    """Authority provider that records how often it is consulted."""

    def __init__(self, authority):
        self.authority = authority
        self.calls = []

    def fetch(self, mint):
        self.calls.append(mint)
        return self.authority


def test_contract_checks_run_only_on_shortlisted_tokens():
    """Each on-chain check is several rate-limited round trips. Running them
    on everything turned a one-minute cycle into ten and left the dashboard
    permanently stale."""

    good = make_snapshot(mint="MintGood", symbol="GOOD")
    rejects = [
        make_snapshot(mint=f"MintThin{i}", symbol=f"T{i}", liquidity_usd=100.0)
        for i in range(40)
    ]
    settings = deterministic_settings(min_entry_score=0.01)
    market = FakeMarket(
        candidates=[good] + rejects,
        prices={s.mint: s for s in [good] + rejects},
    )
    authority = CountingAuthority(safe_authority())
    store = Store(":memory:")
    engine = TradingEngine(
        settings, market, PaperBroker(settings, seed=1),
        RiskManager.start(settings, NOW), store, authority,
    )
    engine.run_cycle(NOW)

    assert len(authority.calls) <= 2, (
        f"consulted the chain {len(authority.calls)} times for 41 tokens"
    )
    assert "MintGood" in authority.calls


def test_the_shortlist_size_is_reported():
    good = make_snapshot(mint="MintGood", symbol="GOOD")
    thin = make_snapshot(mint="MintThin", symbol="THIN", liquidity_usd=50.0)
    settings = deterministic_settings(min_entry_score=0.01)
    market = FakeMarket([good, thin], {s.mint: s for s in (good, thin)})
    engine = TradingEngine(
        settings, market, PaperBroker(settings, seed=1),
        RiskManager.start(settings, NOW), Store(":memory:"),
        CountingAuthority(safe_authority()),
    )
    report = engine.run_cycle(NOW)
    assert report.shortlisted == 1
    assert report.candidates == 1


def test_a_contract_rejection_is_still_bucketed():
    """Phase-two rejections must show on the dashboard like any other."""

    from memecoin_bot.safety import TokenAuthority

    good = make_snapshot(mint="MintGood", symbol="GOOD")
    settings = deterministic_settings(min_entry_score=0.01)
    market = FakeMarket([good], {good.mint: good})
    engine = TradingEngine(
        settings, market, PaperBroker(settings, seed=1),
        RiskManager.start(settings, NOW), Store(":memory:"),
        CountingAuthority(safe_authority(sell_simulation_ok=False)),
    )
    report = engine.run_cycle(NOW)
    assert report.candidates == 0
    assert "unsellable" in report.rejections


def test_unreachable_chain_lookups_are_counted_separately():
    """A rejection caused by an unreachable endpoint looks identical to one
    caused by a dangerous token, and they mean opposite things."""

    from memecoin_bot.onchain import OnChainAuthorityProvider
    from memecoin_bot.safety import TokenAuthority

    class DeadRpc:
        def get_mint_state(self, mint):
            return None
        def get_top_holder_pct(self, mint, supply, *, ignore=frozenset(),
                           exclude_amount=0, tolerance=0.02):
            return None

    class Jup:
        def can_sell(self, mint, amount, *, max_price_impact_pct=0.15):
            return True

    settings = deterministic_settings(min_entry_score=0.01)
    provider = OnChainAuthorityProvider(settings, rpc=DeadRpc(), jupiter=Jup())

    good = make_snapshot(mint="MintGood", symbol="GOOD")
    market = FakeMarket([good], {good.mint: good})
    engine = TradingEngine(
        settings, market, PaperBroker(settings, seed=1),
        RiskManager.start(settings, NOW), Store(":memory:"), provider,
    )
    report = engine.run_cycle(NOW)

    assert report.rpc_lookups >= 1
    assert report.rpc_failures == report.rpc_lookups
    assert report.candidates == 0


def test_a_reachable_chain_reports_no_failures():
    from memecoin_bot.onchain import OnChainAuthorityProvider
    from memecoin_bot.chain import MintState

    class LiveRpc:
        def get_mint_state(self, mint):
            return MintState(True, True, 10**15, 6, "TokenkegQ")
        def get_top_holder_pct(self, mint, supply, *, ignore=frozenset(),
                           exclude_amount=0, tolerance=0.02):
            return 0.04

    class Jup:
        def can_sell(self, mint, amount, *, max_price_impact_pct=0.15):
            return True

    settings = deterministic_settings(min_entry_score=0.01)
    provider = OnChainAuthorityProvider(settings, rpc=LiveRpc(), jupiter=Jup())
    good = make_snapshot(mint="MintGood", symbol="GOOD", liquidity_usd=500_000.0)
    market = FakeMarket([good], {good.mint: good})
    engine = TradingEngine(
        settings, market, PaperBroker(settings, seed=1),
        RiskManager.start(settings, NOW), Store(":memory:"), provider,
    )
    report = engine.run_cycle(NOW)

    assert report.rpc_failures == 0
    assert report.candidates == 1, "a clean token on a live chain must verify"
    assert report.entered == ["GOOD"]


def test_a_healthy_token_with_a_normal_pool_is_bought():
    """End to end, with the pool holding most of the supply as pools do.

    This is the case that failed silently for the entire run: the pool was
    counted as the top holder, so a 70%-in-pool token -- which is completely
    ordinary -- was rejected as a whale on every cycle.
    """

    from memecoin_bot.chain import MintState
    from memecoin_bot.onchain import OnChainAuthorityProvider

    supply = 1_000_000 * 10 ** 6
    pool_units = int(supply * 0.70)

    class Rpc:
        def get_mint_state(self, mint):
            return MintState(True, True, supply, 6, "TokenkegQ")
        def get_top_holder_pct(self, mint, s, *, ignore=frozenset(),
                               exclude_amount=0, tolerance=0.02):
            holdings = [pool_units, int(supply * 0.05)]
            if exclude_amount:
                holdings = [h for h in holdings
                            if abs(h - exclude_amount) > exclude_amount * tolerance]
            return max(holdings) / s

    class Jup:
        def can_sell(self, mint, amount, *, max_price_impact_pct=0.15):
            return True

    settings = deterministic_settings(min_entry_score=0.01)
    provider = OnChainAuthorityProvider(settings, rpc=Rpc(), jupiter=Jup())

    token = make_snapshot(
        mint="MintHealthy", symbol="HEALTHY", liquidity_usd=500_000.0,
        pool_base_amount=700_000.0,
    )
    market = FakeMarket([token], {token.mint: token})
    engine = TradingEngine(
        settings, market, PaperBroker(settings, seed=1),
        RiskManager.start(settings, NOW), Store(":memory:"), provider,
    )
    report = engine.run_cycle(NOW)

    assert report.candidates == 1, f"rejected: {report.rejections}"
    assert report.entered == ["HEALTHY"]


def test_the_same_token_is_rejected_when_the_pool_is_not_declared():
    """Pins the failure mode: no pool balance, and the pool reads as a whale."""

    from memecoin_bot.chain import MintState
    from memecoin_bot.onchain import OnChainAuthorityProvider

    supply = 1_000_000 * 10 ** 6

    class Rpc:
        def get_mint_state(self, mint):
            return MintState(True, True, supply, 6, "TokenkegQ")
        def get_top_holder_pct(self, mint, s, *, ignore=frozenset(),
                               exclude_amount=0, tolerance=0.02):
            holdings = [int(supply * 0.70), int(supply * 0.05)]
            if exclude_amount:
                holdings = [h for h in holdings
                            if abs(h - exclude_amount) > exclude_amount * tolerance]
            return max(holdings) / s

    class Jup:
        def can_sell(self, mint, amount, *, max_price_impact_pct=0.15):
            return True

    settings = deterministic_settings(min_entry_score=0.01)
    provider = OnChainAuthorityProvider(settings, rpc=Rpc(), jupiter=Jup())

    token = make_snapshot(
        mint="MintHealthy", symbol="HEALTHY", liquidity_usd=500_000.0,
        pool_base_amount=0.0,          # not declared
    )
    market = FakeMarket([token], {token.mint: token})
    engine = TradingEngine(
        settings, market, PaperBroker(settings, seed=1),
        RiskManager.start(settings, NOW), Store(":memory:"), provider,
    )
    report = engine.run_cycle(NOW)
    assert report.candidates == 0
    assert "whale" in report.rejections


# --- Advisory verification ----------------------------------------------

def _failing_provider():
    from memecoin_bot.chain import MintState
    from memecoin_bot.onchain import OnChainAuthorityProvider

    class Rpc:
        def get_mint_state(self, mint):
            # Mint authority still live: a real, correct rejection.
            return MintState(False, True, 10 ** 15, 6, "TokenkegQ")
        def get_top_holder_pct(self, mint, s, *, ignore=frozenset(),
                               exclude_amount=0, tolerance=0.02):
            return 0.04

    class Jup:
        def can_sell(self, mint, amount, *, max_price_impact_pct=0.15):
            return True

    return Rpc, Jup, OnChainAuthorityProvider


def test_strict_verification_blocks_an_unsafe_token():
    Rpc, Jup, Provider = _failing_provider()
    settings = deterministic_settings(
        min_entry_score=0.01, verification="strict"
    )
    token = make_snapshot(mint="MintRisky", symbol="RISKY",
                          liquidity_usd=500_000.0, pool_base_amount=700_000.0)
    market = FakeMarket([token], {token.mint: token})
    engine = TradingEngine(
        settings, market, PaperBroker(settings, seed=1),
        RiskManager.start(settings, NOW), Store(":memory:"),
        Provider(settings, rpc=Rpc(), jupiter=Jup()),
    )
    report = engine.run_cycle(NOW)
    assert report.entered == []
    assert "mint-authority" in report.rejections


def test_advisory_verification_buys_and_flags_it():
    """Paper risks nothing, so observing the trade beats refusing it -- but
    the finding must travel with the position, not be discarded."""

    Rpc, Jup, Provider = _failing_provider()
    settings = deterministic_settings(
        min_entry_score=0.01, verification="advisory"
    )
    token = make_snapshot(mint="MintRisky", symbol="RISKY",
                          liquidity_usd=500_000.0, pool_base_amount=700_000.0)
    market = FakeMarket([token], {token.mint: token})
    store = Store(":memory:")
    engine = TradingEngine(
        settings, market, PaperBroker(settings, seed=1),
        RiskManager.start(settings, NOW), store,
        Provider(settings, rpc=Rpc(), jupiter=Jup()),
    )
    report = engine.run_cycle(NOW)

    assert report.entered == ["RISKY"]
    assert report.unverified == 1
    position = engine.positions[0]
    assert position.unverified_reasons, "the finding must be carried"
    assert any("mint authority" in r for r in position.unverified_reasons)


def test_the_unverified_flag_survives_a_restart(tmp_path):
    Rpc, Jup, Provider = _failing_provider()
    path = str(tmp_path / "t.sqlite3")
    settings = deterministic_settings(
        min_entry_score=0.01, verification="advisory", database_path=path
    )
    token = make_snapshot(mint="MintRisky", symbol="RISKY",
                          liquidity_usd=500_000.0, pool_base_amount=700_000.0)
    market = FakeMarket([token], {token.mint: token})

    store = Store(path)
    engine = TradingEngine(
        settings, market, PaperBroker(settings, seed=1),
        RiskManager.start(settings, NOW), store,
        Provider(settings, rpc=Rpc(), jupiter=Jup()),
    )
    engine.run_cycle(NOW)
    store.close()

    restored = Store(path).load_open_positions()[0]
    assert restored.unverified_reasons, "flag must survive persistence"
