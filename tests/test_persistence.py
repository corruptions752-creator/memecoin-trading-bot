"""Tests that state survives a restart.

On a free host that sleeps whenever it is idle, a restart is the normal path,
not an edge case. Anything the bot forgets across one is money or safety lost.
"""

import time

from conftest import FakeMarket, NOW, deterministic_settings, make_snapshot, safe_authority

from memecoin_bot.broker import PaperBroker
from memecoin_bot.engine import TradingEngine
from memecoin_bot.risk import RiskManager
from memecoin_bot.store import Store


class Authority:
    def fetch(self, mint):
        return safe_authority()


def boot(path, market, settings):
    """Start the bot the way __main__ does, restoring saved state."""

    store = Store(path)
    risk = RiskManager.restore(settings, NOW, store)
    engine = TradingEngine(
        settings, market, PaperBroker(settings, seed=1), risk, store, Authority()
    )
    return store, risk, engine


def test_bankroll_is_not_reinvented_on_restart(tmp_path):
    """Regression: cash reset to the full starting balance while positions
    were reloaded, inventing capital on every restart."""

    path = str(tmp_path / "t.sqlite3")
    settings = deterministic_settings(min_entry_score=0.01, database_path=path)
    snapshot = make_snapshot(symbol="HOLD")
    market = FakeMarket([snapshot], {snapshot.mint: snapshot})

    store, risk, engine = boot(path, market, settings)
    engine.run_cycle(NOW)
    assert len(engine.positions) == 1
    cash_before = risk.cash_usd
    cost_before = risk.open_cost_usd
    assert cash_before < 1_000.0
    store.close()

    store2, risk2, engine2 = boot(path, market, settings)
    assert len(engine2.positions) == 1
    assert abs(risk2.cash_usd - cash_before) < 1e-9
    assert abs(risk2.open_cost_usd - cost_before) < 1e-9
    # The invariant that actually matters: capital is conserved.
    assert abs(risk2.bankroll_usd - 1_000.0) < 1.0


def test_a_loss_halt_survives_a_restart(tmp_path):
    """A restart must not quietly re-enable trading after the breaker fired."""

    path = str(tmp_path / "t.sqlite3")
    settings = deterministic_settings(
        min_entry_score=0.01, max_open_positions=1,
        risk_fraction_per_trade=0.20, database_path=path,
    )
    snapshot = make_snapshot(symbol="LOSS")
    market = FakeMarket([snapshot], {snapshot.mint: snapshot})

    store, risk, engine = boot(path, market, settings)
    engine.run_cycle(NOW)
    market.set_price(snapshot.mint, 0.0001)
    engine.run_cycle(NOW + 300)
    assert risk.halted
    store.close()

    _, risk2, _ = boot(path, market, settings)
    assert risk2.halted
    assert "daily loss limit" in risk2.halt_reason


def test_realized_pnl_for_the_day_survives_a_restart(tmp_path):
    path = str(tmp_path / "t.sqlite3")
    settings = deterministic_settings(min_entry_score=0.01, database_path=path)
    snapshot = make_snapshot(symbol="DIP")
    market = FakeMarket([snapshot], {snapshot.mint: snapshot})

    store, risk, engine = boot(path, market, settings)
    engine.run_cycle(NOW)
    market.set_price(snapshot.mint, 0.0005)
    engine.run_cycle(NOW + 300)
    realized = risk.realized_today_usd
    assert realized < 0
    store.close()

    _, risk2, _ = boot(path, market, settings)
    assert abs(risk2.realized_today_usd - realized) < 1e-9


def test_a_first_run_starts_from_the_configured_bankroll(tmp_path):
    path = str(tmp_path / "fresh.sqlite3")
    settings = deterministic_settings(
        starting_bankroll_usd=250.0, database_path=path
    )
    store = Store(path)
    risk = RiskManager.restore(settings, NOW, store)
    assert risk.cash_usd == 250.0
    assert risk.bankroll_usd == 250.0


def test_the_first_run_persists_immediately(tmp_path):
    """So a crash before the first trade does not lose the starting state."""

    path = str(tmp_path / "fresh.sqlite3")
    settings = deterministic_settings(starting_bankroll_usd=250.0)
    store = Store(path)
    RiskManager.restore(settings, NOW, store)
    assert store.load_risk_state() is not None


def test_restore_without_a_store_starts_fresh():
    settings = deterministic_settings()
    risk = RiskManager.restore(settings, NOW, None)
    assert risk.cash_usd == settings.starting_bankroll_usd


def test_the_classmethod_does_not_shadow_the_halt_clear():
    """restore() and resume() are different operations and must both work."""

    risk = RiskManager.start(deterministic_settings(), NOW)
    risk.halt("manual")
    risk.resume()
    assert not risk.halted


def test_capital_is_conserved_across_many_restarts(tmp_path):
    """Ten restarts must not drift the bankroll at all."""

    path = str(tmp_path / "t.sqlite3")
    settings = deterministic_settings(min_entry_score=0.01, database_path=path)
    snapshot = make_snapshot(symbol="LOOP")
    market = FakeMarket([snapshot], {snapshot.mint: snapshot})

    store, risk, engine = boot(path, market, settings)
    engine.run_cycle(NOW)
    store.close()

    for _ in range(10):
        store, risk, engine = boot(path, market, settings)
        store.close()

    assert abs(risk.bankroll_usd - 1_000.0) < 1.0


def test_the_runner_database_is_not_gitignored():
    """A scheduled run starts from a fresh checkout, so the committed
    database is the bot's only memory between cycles.

    A `*.sqlite3` rule silently excluded it for several runs: the bankroll
    reset every cycle, the re-entry blocklist emptied, and any open position
    would have been orphaned -- bought, forgotten, never sold.
    """

    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    if not (repo / ".git").exists():
        return  # not a checkout; nothing to assert

    result = subprocess.run(
        ["git", "check-ignore", "state/trading.sqlite3"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode != 0, (
        "state/trading.sqlite3 is gitignored; the scheduled runner would "
        "lose all state between cycles"
    )


def test_local_databases_are_still_ignored():
    """Only the runner's path is exempt; a local run must not be committed."""

    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    if not (repo / ".git").exists():
        return

    result = subprocess.run(
        ["git", "check-ignore", "memecoin_bot/data/trading.sqlite3"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, "local databases should stay ignored"


def test_a_database_from_an_older_version_is_migrated(tmp_path):
    """CREATE TABLE IF NOT EXISTS does nothing to an existing table, so a
    file written before a column was added keeps its old shape and every
    read of that column raises. It crashed five consecutive scheduled runs.
    """

    import sqlite3
    from memecoin_bot.store import Store

    path = str(tmp_path / "old.sqlite3")
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE activity (
            id INTEGER PRIMARY KEY CHECK (id = 1), at REAL NOT NULL,
            scanned INTEGER NOT NULL, rejected INTEGER NOT NULL,
            candidates INTEGER NOT NULL, entered INTEGER NOT NULL,
            rejections TEXT NOT NULL DEFAULT '{}',
            skipped_reason TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO activity VALUES (1, 1.0, 100, 99, 1, 0, '{}', '');
        """
    )
    legacy.commit()
    legacy.close()

    activity = Store(path).load_activity()
    assert activity is not None
    assert activity["scanned"] == 100, "existing data must survive"
    assert activity["rpc_failures"] == 0, "new column must default"


def test_migrated_columns_accept_writes(tmp_path):
    import sqlite3
    from memecoin_bot.store import Store

    path = str(tmp_path / "old.sqlite3")
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE activity (
            id INTEGER PRIMARY KEY CHECK (id = 1), at REAL NOT NULL,
            scanned INTEGER NOT NULL, rejected INTEGER NOT NULL,
            candidates INTEGER NOT NULL, entered INTEGER NOT NULL,
            rejections TEXT NOT NULL DEFAULT '{}',
            skipped_reason TEXT NOT NULL DEFAULT ''
        );
        """
    )
    legacy.commit()
    legacy.close()

    store = Store(path)
    store.save_activity(
        at=1.0, scanned=5, rejected=4, candidates=1, entered=0,
        rejections={}, shortlisted=2, rpc_failures=1, rpc_lookups=3,
    )
    activity = store.load_activity()
    assert activity["rpc_lookups"] == 3
    assert activity["shortlisted"] == 2
