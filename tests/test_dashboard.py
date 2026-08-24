"""Tests for the web dashboard.

The dashboard's one job is to show what the bot actually did, so these check
that the served state matches the database rather than checking pixels.
"""

import json
import threading
import urllib.request

from conftest import FakeMarket, NOW, deterministic_settings, make_snapshot, safe_authority

from memecoin_bot.broker import PaperBroker
from memecoin_bot.dashboard import build_state, serve
from memecoin_bot.engine import TradingEngine
from memecoin_bot.risk import RiskManager
from memecoin_bot.store import Store


class Authority:
    def fetch(self, mint):
        return safe_authority()


def traded(tmp_path, cycles=1, crash_price=None):
    """A store holding the result of some real paper trading."""

    path = str(tmp_path / "dash.sqlite3")
    settings = deterministic_settings(min_entry_score=0.01, database_path=path)
    snapshot = make_snapshot(symbol="DASH")
    market = FakeMarket([snapshot], {snapshot.mint: snapshot})
    store = Store(path)
    risk = RiskManager.restore(settings, NOW, store)
    engine = TradingEngine(
        settings, market, PaperBroker(settings, seed=1), risk, store, Authority()
    )
    engine.run_cycle(NOW)
    if crash_price is not None:
        market.set_price(snapshot.mint, crash_price)
        engine.run_cycle(NOW + 300)
    risk.persist(store, NOW + 600)
    return settings, store


def test_state_reports_an_open_position(tmp_path):
    settings, store = traded(tmp_path)
    state = build_state(settings, store)
    assert len(state["positions"]) == 1
    assert state["positions"][0]["symbol"] == "DASH"


def test_state_includes_the_ladder_levels(tmp_path):
    """The page draws stop and target, so they must be served."""

    settings, store = traded(tmp_path)
    position = build_state(settings, store)["positions"][0]
    assert position["stop_price"] < position["entry_price"]
    assert position["target_price"] > position["entry_price"]


def test_state_reflects_capital_leaving_cash(tmp_path):
    settings, store = traded(tmp_path)
    state = build_state(settings, store)
    assert state["cash"] < state["starting_bankroll"]
    assert state["open_cost"] > 0


def test_a_closed_trade_appears_with_its_reason(tmp_path):
    settings, store = traded(tmp_path, crash_price=0.0005)
    state = build_state(settings, store)
    assert state["closed"]
    assert state["closed"][0]["reason"] == "stop_loss"
    assert state["closed"][0]["realized_usd"] < 0


def test_the_equity_curve_starts_at_starting_capital(tmp_path):
    settings, store = traded(tmp_path, crash_price=0.0005)
    state = build_state(settings, store)
    assert state["equity"][0] == state["starting_bankroll"]
    assert len(state["equity"]) == len(state["closed"]) + 1


def test_fills_are_served_newest_first(tmp_path):
    settings, store = traded(tmp_path, crash_price=0.0005)
    fills = build_state(settings, store)["recent_fills"]
    assert fills[0]["side"] == "sell"
    assert fills[-1]["side"] == "buy"


def test_stats_are_included(tmp_path):
    settings, store = traded(tmp_path, crash_price=0.0005)
    stats = build_state(settings, store)["stats"]
    for key in ("trades", "win_rate", "max_drawdown", "expectancy_usd"):
        assert key in stats


def test_an_infinite_profit_factor_is_serialisable(tmp_path):
    """float('inf') is not valid JSON; it must be sent as null."""

    settings, store = traded(tmp_path)
    state = build_state(settings, store)
    json.dumps(state)  # must not raise


def test_a_halt_is_surfaced(tmp_path):
    settings, store = traded(tmp_path)
    store.save_risk_state(
        cash_usd=900.0, open_cost_usd=0.0, realized_today_usd=-60.0,
        current_day="2026-08-23", halted=True,
        halt_reason="daily loss limit hit", at=NOW,
    )
    state = build_state(settings, store)
    assert state["halted"] is True
    assert "daily loss limit" in state["halt_reason"]


def test_a_fresh_database_serves_sane_defaults(tmp_path):
    settings = deterministic_settings(
        database_path=str(tmp_path / "empty.sqlite3")
    )
    state = build_state(settings, Store(settings.database_path))
    assert state["positions"] == []
    assert state["closed"] == []
    assert state["bankroll"] == settings.starting_bankroll_usd
    json.dumps(state)


# --- HTTP ----------------------------------------------------------------

def test_the_server_serves_page_and_state(tmp_path):
    settings, store = traded(tmp_path)
    store.close()
    server = serve(settings, host="127.0.0.1", port=0, background=True)
    port = server.server_address[1]
    try:
        page = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/", timeout=5
        ).read().decode()
        assert "<title>Trading Bot</title>" in page
        assert "/api/state" in page

        payload = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/state", timeout=5
        ).read().decode())
        assert payload["positions"][0]["symbol"] == "DASH"
    finally:
        server.shutdown()
        server.server_close()


def test_the_favicon_does_not_404(tmp_path):
    """A 404 here only litters the browser console."""

    settings, store = traded(tmp_path)
    store.close()
    server = serve(settings, host="127.0.0.1", port=0, background=True)
    port = server.server_address[1]
    try:
        response = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/favicon.ico", timeout=5
        )
        assert response.status == 204
    finally:
        server.shutdown()
        server.server_close()


def test_an_unknown_path_is_404(tmp_path):
    settings, store = traded(tmp_path)
    store.close()
    server = serve(settings, host="127.0.0.1", port=0, background=True)
    port = server.server_address[1]
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as error:
            assert error.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_the_page_is_self_contained():
    """No CDN, no external fonts -- it must work with no internet.

    The SVG namespace URI is excluded: it is a required constant for
    createElementNS and is never fetched.
    """

    from pathlib import Path
    import re
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    urls = re.findall(r"https?://[^\s\"'<>]+", page)
    external = [u for u in urls if not u.startswith("http://www.w3.org/")]
    assert not external, f"external references: {external}"

    for forbidden in ("<script src", "<link rel=\"stylesheet\"", "@import"):
        assert forbidden not in page, f"external asset: {forbidden}"


def test_the_page_declares_both_themes():
    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    assert "prefers-color-scheme: dark" in page
    assert '[data-theme="dark"]' in page


def test_gains_and_losses_are_not_colour_alone():
    """Status green and red are close under deuteranopia, so direction must
    also be carried by a glyph and a sign."""

    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    assert "▲" in page and "▼" in page


# --- Static snapshot export ---------------------------------------------

def test_export_writes_a_snapshot_and_the_page(tmp_path):
    """A scheduled runner has no server, so the page and its data ship
    together as files."""

    import logging
    from memecoin_bot.__main__ import _export

    settings, store = traded(tmp_path)
    out = tmp_path / "pages" / "state.json"
    code = _export(settings, store, str(out), logging.getLogger("t"))

    assert code == 0
    assert out.exists()
    assert (out.parent / "index.html").exists()


def test_the_snapshot_matches_the_live_state(tmp_path):
    import json
    import logging
    from memecoin_bot.__main__ import _export

    settings, store = traded(tmp_path)
    out = tmp_path / "state.json"
    _export(settings, store, str(out), logging.getLogger("t"))

    snapshot = json.loads(out.read_text())
    live = build_state(settings, store)
    assert snapshot["positions"][0]["symbol"] == live["positions"][0]["symbol"]
    assert snapshot["bankroll"] == live["bankroll"]


def test_the_snapshot_carries_its_own_timestamp(tmp_path):
    """The static page must be able to say how stale it is."""

    import json
    import logging
    from memecoin_bot.__main__ import _export

    settings, store = traded(tmp_path)
    out = tmp_path / "state.json"
    _export(settings, store, str(out), logging.getLogger("t"))
    assert json.loads(out.read_text())["generated_at"] > 0


def test_the_page_falls_back_to_the_snapshot():
    """Served statically there is no /api/state, so it must retry state.json
    and stop claiming to be live."""

    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    assert '"state.json"' in page
    assert "staticMode" in page
    assert "snapshot ·" in page


def test_the_page_busts_the_cdn_cache():
    """GitHub Pages caches for minutes; `no-store` only covers the browser's
    own cache, so a unique URL per request is what actually gets fresh data."""

    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    assert "function bust(" in page
    assert "bust(endpoint)" in page
    assert "Date.now()" in page


def test_the_page_refreshes_when_reopened():
    """Phones suspend timers in background tabs, so returning to the page
    must fetch rather than show what it held when it was put away."""

    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    assert "visibilitychange" in page
    assert "pageshow" in page


# --- Scan history and status strip ---------------------------------------

def test_scan_history_is_recorded_and_served(tmp_path):
    """One number cannot show a trend; the page plots recent cycles."""

    settings, store = traded(tmp_path)
    # traded() runs a cycle, which records one row of its own.
    before = len(store.activity_history())
    for i in range(5):
        store.save_activity(
            at=NOW + 100 + i, scanned=100 + i * 10, rejected=90, candidates=i,
            entered=0, rejections={"liquidity": 90},
        )
    history = build_state(settings, store)["scan_history"]
    assert len(history) == before + 5
    assert [h["scanned"] for h in history[-5:]] == [100, 110, 120, 130, 140]


def test_scan_history_is_bounded(tmp_path):
    """A run every 15 minutes for weeks must not grow without limit."""

    settings, store = traded(tmp_path)
    for i in range(260):
        store.save_activity(
            at=NOW + i, scanned=100, rejected=100, candidates=0,
            entered=0, rejections={},
        )
    rows = store._connection.execute(
        "SELECT COUNT(*) AS n FROM activity_history"
    ).fetchone()
    assert rows["n"] <= 200


def test_scan_history_is_oldest_first(tmp_path):
    settings, store = traded(tmp_path)
    for i in range(4):
        store.save_activity(
            at=NOW + 100 + i, scanned=i, rejected=0, candidates=0, entered=0,
            rejections={},
        )
    timestamps = [h["at"] for h in store.activity_history()]
    assert timestamps == sorted(timestamps), "history must be oldest first"


def test_the_page_renders_a_trend_and_a_next_check():
    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    assert "drawSpark" in page
    assert "renderStrip" in page
    assert "overdue" in page, "a stalled runner must be visible"


def test_empty_states_explain_rather_than_look_broken():
    """With no trades the page is mostly empty; it must read as working and
    waiting, not as failed."""

    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    assert "Nothing held right now" in page
    assert "No completed trades yet" in page
    assert "No orders placed yet" in page


# --- Run progress --------------------------------------------------------

def test_run_progress_is_reported(tmp_path):
    """Over a three-week test most cycles do nothing, so accumulated
    progress is what separates 'working' from 'stuck'."""

    settings, store = traded(tmp_path)
    for i in range(10):
        store.save_activity(
            at=NOW + i * 900, scanned=200, rejected=200, candidates=0,
            entered=0, rejections={}, shortlisted=1,
        )
    run = build_state(settings, store)["run"]
    assert run["cycles"] >= 10
    assert run["tokens_seen"] >= 2_000
    assert run["started_at"] is not None


def test_a_fresh_database_reports_no_progress(tmp_path):
    settings = deterministic_settings(
        database_path=str(tmp_path / "empty.sqlite3")
    )
    run = build_state(settings, Store(settings.database_path))["run"]
    assert run["cycles"] == 0
    assert run["started_at"] is None


def test_the_page_renders_the_run_progress():
    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    assert "renderProgress" in page
    assert "RUN_DAYS" in page


def test_the_funnel_shows_the_shortlist_step():
    """Seen -> shortlist -> verified -> bought localises where candidates
    are lost, which one number could not."""

    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    for label in ("seen", "shortlist", "verified", "bought"):
        assert f'"{label}"' in page


def test_an_unchanged_equity_says_so_plainly():
    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    assert "nothing traded yet" in page


# --- Risk posture --------------------------------------------------------

def test_the_active_risk_posture_is_served(tmp_path):
    """Sizing that aggressive is a deliberate choice, so the page must say
    which posture is running rather than leaving it implicit."""

    from memecoin_bot.config import Settings

    settings = Settings(
        profile="aggressive", risk_fraction_per_trade=0.05,
        stop_loss_pct=0.35, take_profit_multiple=3.0,
        database_path=str(tmp_path / "t.sqlite3"),
    )
    state = build_state(settings, Store(settings.database_path))
    assert state["profile"] == "aggressive"
    assert state["risk"]["per_trade_pct"] == 0.05
    assert state["risk"]["stop_pct"] == 0.35


def test_the_page_shows_the_posture():
    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    assert 'id="profile"' in page
    assert "per trade" in page
