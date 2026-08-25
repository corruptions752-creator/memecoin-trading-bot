"""Tests for persistence across restarts."""

from conftest import NOW, make_snapshot

from memecoin_bot.models import ExitReason, Fill, Position, Side
from memecoin_bot.store import Store


def make_position(**overrides) -> Position:
    defaults = dict(
        mint="Mint111",
        symbol="TEST",
        entry_price_usd=0.001,
        quantity=10_000.0,
        cost_usd=10.0,
        opened_at=NOW,
        entry_liquidity_usd=100_000.0,
    )
    defaults.update(overrides)
    return Position(**defaults)


def test_open_position_assigns_an_id():
    store = Store(":memory:")
    position = store.open_position(make_position())
    assert position.position_id is not None


def test_open_positions_survive_a_reload(tmp_path):
    path = str(tmp_path / "trading.sqlite3")
    store = Store(path)
    store.open_position(make_position(symbol="ALPHA"))
    store.open_position(make_position(mint="Mint222", symbol="BETA"))
    store.close()

    reopened = Store(path)
    positions = reopened.load_open_positions()
    assert [p.symbol for p in positions] == ["ALPHA", "BETA"]
    assert positions[0].quantity == 10_000.0


def test_closed_positions_are_excluded_from_open():
    store = Store(":memory:")
    position = store.open_position(make_position())
    store.close_position(position, ExitReason.STOP_LOSS, NOW + 60)
    assert store.load_open_positions() == []
    assert len(store.closed_positions()) == 1


def test_update_persists_partial_exit_state(tmp_path):
    path = str(tmp_path / "trading.sqlite3")
    store = Store(path)
    position = store.open_position(make_position())
    position.quantity = 5_000.0
    position.took_first_profit = True
    position.peak_price_usd = 0.004
    position.realized_usd = 7.5
    store.update_position(position)
    store.close()

    restored = Store(path).load_open_positions()[0]
    assert restored.quantity == 5_000.0
    assert restored.took_first_profit is True
    assert restored.peak_price_usd == 0.004
    assert restored.realized_usd == 7.5
    assert restored.initial_quantity == 10_000.0


def test_fills_round_trip():
    store = Store(":memory:")
    position = store.open_position(make_position())
    store.record_fill(
        Fill(
            mint="Mint111", symbol="TEST", side=Side.BUY, price_usd=0.001,
            quantity=10_000.0, gross_usd=10.0, fee_usd=0.05,
            slippage_usd=0.15, at=NOW, reason="score=0.7",
        ),
        position.position_id,
    )
    fills = store.all_fills()
    assert len(fills) == 1
    assert fills[0].side is Side.BUY
    assert fills[0].reason == "score=0.7"


def test_close_reason_is_recorded():
    store = Store(":memory:")
    position = store.open_position(make_position())
    store.close_position(position, ExitReason.TRAILING_STOP, NOW + 60)
    assert store.closed_positions()[0]["close_reason"] == "trailing_stop"


def test_updating_an_unstored_position_is_an_error():
    store = Store(":memory:")
    try:
        store.update_position(make_position())
    except ValueError as error:
        assert "never stored" in str(error)
    else:
        raise AssertionError("expected ValueError")


# --- Scanner snapshots ----------------------------------------------------

def test_the_scan_replaces_rather_than_accumulates():
    """A token card carries a live price. Keeping a token that dropped out
    of the feed would show an hour-old price as if it were current."""

    store = Store(":memory:")
    store.save_scan_tokens(
        [{"mint": "a", "symbol": "A", "confidence": 0.1},
         {"mint": "b", "symbol": "B", "confidence": 0.2}], 100.0)
    store.save_scan_tokens([{"mint": "c", "symbol": "C", "confidence": 0.3}], 200.0)

    assert [t["symbol"] for t in store.scan_tokens()] == ["C"]


def test_scan_tokens_come_back_highest_confidence_first():
    store = Store(":memory:")
    store.save_scan_tokens([
        {"mint": "a", "symbol": "LOW", "confidence": 0.1},
        {"mint": "b", "symbol": "HIGH", "confidence": 0.9},
        {"mint": "c", "symbol": "MID", "confidence": 0.5},
    ], 100.0)
    assert [t["symbol"] for t in store.scan_tokens()] == ["HIGH", "MID", "LOW"]


def test_a_scan_entry_without_a_mint_is_dropped():
    store = Store(":memory:")
    store.save_scan_tokens([{"symbol": "NOMINT", "confidence": 0.5}], 100.0)
    assert store.scan_tokens() == []


def test_an_empty_scan_clears_the_previous_one():
    store = Store(":memory:")
    store.save_scan_tokens([{"mint": "a", "symbol": "A"}], 100.0)
    store.save_scan_tokens([], 200.0)
    assert store.scan_tokens() == []


# --- Event log ------------------------------------------------------------

def test_events_come_back_newest_first():
    store = Store(":memory:")
    store.record_event("buy", "bought A", 1.0, symbol="A")
    store.record_event("sell", "sold A", 2.0, symbol="A")
    assert [e["message"] for e in store.recent_events()] == ["sold A", "bought A"]


def test_an_event_keeps_its_detail():
    store = Store(":memory:")
    store.record_event("buy", "bought A", 1.0, symbol="A", detail={"usd": 52.0})
    assert store.recent_events()[0]["detail"] == {"usd": 52.0}


def test_heartbeats_are_trimmed_before_trades():
    """A cycle writes a scan line every time. Under one flat cap the buys
    and sells -- the only entries worth keeping -- scroll out within hours.
    """

    store = Store(":memory:")
    store.record_event("buy", "bought EARLY", 1.0, symbol="EARLY")
    store.record_events([
        {"at": 10.0 + i, "kind": "scan", "message": f"scan {i}"}
        for i in range(200)
    ])

    events = store.recent_events(limit=500)
    kinds = [e["kind"] for e in events]
    assert kinds.count("scan") == Store._HEARTBEAT_LIMIT
    assert "bought EARLY" in [e["message"] for e in events]


def test_the_event_log_stays_bounded():
    store = Store(":memory:")
    store.record_events([
        {"at": float(i), "kind": "buy", "message": f"buy {i}"}
        for i in range(Store._EVENT_LIMIT + 120)
    ])
    assert len(store.recent_events(limit=10_000)) == Store._EVENT_LIMIT


def test_recording_no_events_is_a_no_op():
    store = Store(":memory:")
    store.record_events([])
    assert store.recent_events() == []
