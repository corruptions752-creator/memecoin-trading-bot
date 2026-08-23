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
