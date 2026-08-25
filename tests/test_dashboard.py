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
        assert "<title>Memecoin Terminal</title>" in page
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
    assert "bust(source)" in page, "the fetch must go through the buster"
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
    assert "No open positions" in page
    assert "No completed trades" in page
    assert "No orders yet" in page


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
    assert "DAY ${day}" in page


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


def test_near_misses_are_recorded_and_served(tmp_path):
    """When nothing trades, the most useful thing is which token came
    closest and exactly what stopped it. A bucket count says how many; this
    says which."""

    settings, store = traded(tmp_path)
    store.save_activity(
        at=NOW, scanned=200, rejected=199, candidates=0, entered=0,
        rejections={"mint-authority": 1}, shortlisted=1,
        near_misses=[["WIF", ["mint authority not confirmed revoked"]]],
    )
    activity = build_state(settings, store)["activity"]
    assert activity["near_misses"][0][0] == "WIF"
    assert "mint authority" in activity["near_misses"][0][1][0]


def test_near_misses_default_on_an_older_database(tmp_path):
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
        INSERT INTO activity VALUES (1, 1.0, 10, 9, 1, 0, '{}', '');
        """
    )
    legacy.commit(); legacy.close()
    assert Store(path).load_activity()["near_misses"] == []


def test_the_page_carries_its_own_favicon():
    """Served statically there is no 204 route, so a missing icon logs a 404
    on every load. An inline data URI keeps the page self-contained."""

    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    assert 'rel="icon"' in page
    assert "data:image/svg+xml" in page


def test_the_ticker_marks_direction_without_relying_on_colour():
    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    assert "renderTicker" in page
    assert "\\u25B2" in page and "\\u25BC" in page


def test_the_page_has_no_undefined_helpers():
    """A splice that removes a helper leaves a page that parses but dies on
    first use. It happened: bust() was deleted and every fetch threw.

    Checks that each helper the script calls is also defined in it.
    """

    import re
    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    script = page[page.index("<script>"):page.index("</script>")]

    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)", script))
    defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", script))
    # Parameter names are callable inside their function (a formatter passed
    # in, for instance), so they count as defined.
    for params in re.findall(r"function\s*[\w$]*\s*\(([^)]*)\)", script):
        for param in params.split(","):
            name = param.split("=")[0].strip()
            if name.isidentifier():
                defined.add(name)

    builtin = {
        "fetch", "setInterval", "setTimeout", "parseInt", "parseFloat",
        "isNaN", "String", "Number", "Math", "Object", "Array", "Promise",
        "Date", "JSON", "console", "document", "window", "escape",
        # Keywords that the call-site regex cannot tell from a call.
        "if", "for", "while", "switch", "catch", "return", "typeof",
        "await", "of", "in", "new", "else", "var", "let", "const",
    }
    called = set(re.findall(r"\b([a-z][\w$]*)\s*\(", script)) - builtin

    # Only flag names that look like our own helpers: defined nowhere and
    # not a method call (those are preceded by a dot, excluded by \b above).
    suspects = {
        name for name in called
        if name not in defined and f".{name}(" not in script
        and f"{name}:" not in script
    }
    assert not suspects, f"called but never defined: {sorted(suspects)}"


# --- Marking positions to market -----------------------------------------

def test_an_open_position_is_marked_at_the_scanners_price(tmp_path):
    """Without a live mark, portfolio value is stuck at cost and a position
    that has doubled looks identical to one that has not moved."""

    settings, store = traded(tmp_path)
    position = build_state(settings, store)["positions"][0]

    assert position["priced"] is True
    assert position["price"] > 0
    assert position["value_usd"] == position["quantity"] * position["price"]
    assert position["unrealized_usd"] == (
        position["value_usd"] - position["cost_usd"]
    )


def test_equity_counts_the_marked_value_not_the_cost(tmp_path):
    settings, store = traded(tmp_path)
    state = build_state(settings, store)
    assert state["equity_usd"] == state["cash"] + state["marked_value"]
    assert state["unrealized_usd"] == state["marked_value"] - state["open_cost"]


def test_an_unpriced_position_is_carried_at_cost_and_says_so(tmp_path):
    """A stale price is worse than an honest gap: it puts a number the bot
    cannot stand behind into the headline figure."""

    settings, store = traded(tmp_path)
    store.save_scan_tokens([], NOW + 700)

    position = build_state(settings, store)["positions"][0]
    assert position["priced"] is False
    assert position["price"] is None
    assert position["value_usd"] == position["cost_usd"]


# --- Risk monitor ---------------------------------------------------------

def test_the_risk_monitor_measures_exposure(tmp_path):
    settings, store = traded(tmp_path)
    state = build_state(settings, store)
    monitor = state["risk_monitor"]

    assert monitor["slots"] == settings.max_open_positions
    assert monitor["slots_used"] == len(state["positions"])
    assert 0 < monitor["exposure_pct"] < 1
    assert monitor["concentration_pct"] <= monitor["exposure_pct"] + 1e-9


def test_the_daily_budget_is_reported_against_the_configured_limit(tmp_path):
    settings, store = traded(tmp_path, crash_price=0.0005)
    monitor = build_state(settings, store)["risk_monitor"]

    assert monitor["daily_loss_limit_usd"] > 0
    assert monitor["lost_today_usd"] >= 0
    assert monitor["daily_budget_used_pct"] == (
        monitor["lost_today_usd"] / monitor["daily_loss_limit_usd"]
    )


def test_the_stop_risk_is_the_whole_book_at_its_stop(tmp_path):
    settings, store = traded(tmp_path)
    state = build_state(settings, store)
    monitor = state["risk_monitor"]

    expected = sum(p["value_usd"] for p in state["positions"]) * settings.stop_loss_pct
    assert monitor["stop_risk_usd"] == expected


def test_an_unverified_position_is_named_in_the_monitor(tmp_path):
    from memecoin_bot.models import Position

    settings, store = traded(tmp_path)
    store.open_position(Position(
        mint="Unverified1111", symbol="SHADY", entry_price_usd=0.001,
        quantity=1_000.0, cost_usd=50.0, opened_at=NOW,
        entry_liquidity_usd=50_000.0, peak_price_usd=0.001,
        unverified_reasons=("mint authority not revoked",),
    ))
    monitor = build_state(settings, store)["risk_monitor"]
    assert monitor["unverified_count"] == 1
    assert monitor["unverified_symbols"] == ["SHADY"]


def test_a_fresh_database_reports_a_calm_risk_monitor(tmp_path):
    settings = deterministic_settings(
        database_path=str(tmp_path / "empty.sqlite3")
    )
    monitor = build_state(settings, Store(settings.database_path))["risk_monitor"]
    assert monitor["exposure_pct"] == 0
    assert monitor["unverified_count"] == 0
    assert monitor["danger_count"] == 0


# --- Scanner and events on the wire --------------------------------------

def test_the_scan_and_the_event_log_are_served(tmp_path):
    settings, store = traded(tmp_path)
    state = build_state(settings, store)

    assert state["scanner"], "the terminal has nothing to draw without this"
    assert state["events"], "the activity feed has nothing to draw without this"
    assert state["scanner"][0]["symbol"] == "DASH"


def test_the_entry_threshold_is_served_so_the_page_can_name_it(tmp_path):
    settings, store = traded(tmp_path)
    assert build_state(settings, store)["risk"]["min_entry_score"] == (
        settings.min_entry_score
    )


# --- The page renders the new sections -----------------------------------

def page_text():
    from pathlib import Path
    import memecoin_bot

    return (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()


def test_the_page_renders_every_section_the_state_carries():
    page = page_text()
    for renderer in (
        "renderCards", "renderSignals", "renderRisk", "renderTerminal",
        "renderFunnel", "renderPositions", "renderClosed", "renderFills",
    ):
        assert f"function {renderer}" in page
        assert f"{renderer}(" in page.split(f"function {renderer}")[1], (
            f"{renderer} is defined but never called"
        )


def test_the_scanner_explains_an_unchecked_contract():
    """Advisory mode and a full book both leave contract checks unread. A
    card that showed nothing there would read as a clean bill of health."""

    assert "not checked" in page_text()


def test_a_meter_bar_never_stands_alone():
    """A bar with no number is decoration; the reader cannot act on it."""

    page = page_text()
    assert "function bar(" in page
    assert "mtop" in page and "gtop" in page


def test_a_zero_bar_is_not_drawn():
    """A minimum width makes a 1% reading visible, and would otherwise paint
    a coloured nub on a gauge reading zero."""

    page = page_text()
    assert "if (percent <= 0) return" in page


def test_tables_collapse_rather_than_scroll_on_a_phone():
    """Six columns on a 390px screen push P&L off the side, so the number
    the reader came for needs a sideways swipe to find."""

    page = page_text()
    assert "rtable" in page
    assert 'data-l="P&L"' in page
    assert "content: attr(data-l)" in page


def test_the_activity_feed_shows_the_kind_of_each_event():
    page = page_text()
    assert "class=\"kind" in page
    assert ".kind.buy" in page and ".kind.sell" in page


def test_a_skipped_verification_is_not_reported_as_zero_passing(tmp_path):
    """With no free slot the contract checks never run, and a candidate
    count of zero then means "not asked" rather than "everything failed".
    Those are opposite readings of the same number."""

    settings, store = traded(tmp_path)
    store.save_activity(
        at=NOW, scanned=300, rejected=295, candidates=0, entered=0,
        rejections={}, shortlisted=5, verification_ran=False,
        skipped_reason="at position limit (8)",
    )
    activity = build_state(settings, store)["activity"]
    assert activity["verification_ran"] is False

    page = page_text()
    assert "verification_ran !== false" in page
    assert "Contract checks were skipped" in page


def test_an_older_database_reports_verification_as_having_run(tmp_path):
    """The column did not exist before; defaulting it to false would
    retro-label every recorded scan as skipped."""

    settings, store = traded(tmp_path)
    store.save_activity(
        at=NOW, scanned=300, rejected=295, candidates=2, entered=1,
        rejections={}, shortlisted=5,
    )
    assert build_state(settings, store)["activity"]["verification_ran"] is True


def _script_functions(script):
    """Split a script into (name, body) for each top-level function.

    Brace counting survives template literals because the braces in `${...}`
    are themselves balanced.
    """

    import re

    out = []
    for match in re.finditer(r"function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{", script):
        depth, i = 1, match.end()
        while i < len(script) and depth:
            if script[i] == "{":
                depth += 1
            elif script[i] == "}":
                depth -= 1
            i += 1
        out.append((match.group(1), match.group(2),
                    script[match.end():i - 1], match.start(), i))
    return out


def test_the_page_has_no_undefined_template_variables():
    """The helper guard checks called names, so a splice that removes a
    `const` leaves a page that parses and dies on first render. It happened:
    a sparkline rewrite deleted `entries`, the caption below still read it,
    and the whole suite stayed green -- a second `entries` in another
    function was enough to fool a scope-blind check.

    So this one is per-function: a name read inside a function's template
    interpolations must be bound in that function, in its parameters, or at
    module scope.
    """

    import re
    from pathlib import Path
    import memecoin_bot

    page = (Path(memecoin_bot.__file__).parent / "dashboard.html").read_text()
    script = page[page.index("<script>"):page.index("</script>")]

    functions = _script_functions(script)
    assert len(functions) > 15, "the function splitter stopped working"

    def bindings(text):
        found = set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)", text))
        found |= set(re.findall(r"function\s+([A-Za-z_$][\w$]*)", text))
        found |= set(re.findall(r"(?:^|[^\w$])([A-Za-z_$][\w$]*)\s*=>", text))
        for params in re.findall(r"\(([^()]*)\)\s*=>", text):
            found |= {q.split("=")[0].strip() for q in params.split(",")}
        for params in re.findall(r"function\s*[\w$]*\s*\(([^)]*)\)", text):
            found |= {q.split("=")[0].strip() for q in params.split(",")}
        for name in re.findall(r"catch\s*\(\s*([\w$]+)", text):
            found.add(name)
        return {f for f in found if f}

    # Cut each function body out by span. Removing their concatenation
    # instead matches nothing, which quietly makes every local a global and
    # defeats the whole check.
    module_text = script
    for _, _, _, begin, finish in sorted(functions, key=lambda f: -f[3]):
        module_text = module_text[:begin] + module_text[finish:]
    module_scope = bindings(module_text)
    module_scope |= {name for name, _, _, _, _ in functions}

    builtin = {
        "Math", "Object", "Array", "JSON", "Date", "String", "Number",
        "Promise", "document", "window", "console", "isNaN", "parseInt",
        "parseFloat", "true", "false", "null", "undefined", "this",
    }

    problems = []
    for name, params, body, _, _ in functions:
        allowed = module_scope | builtin | bindings(body)
        allowed |= {q.split("=")[0].strip() for q in params.split(",") if q.strip()}
        for expression in re.findall(r"\$\{([^{}]*)\}", body):
            match = re.match(r"\s*([A-Za-z_$][\w$]*)", expression)
            if match and match.group(1) not in allowed:
                problems.append(f"{name}() reads {match.group(1)}")

    assert not problems, f"read but not bound in scope: {sorted(set(problems))}"
