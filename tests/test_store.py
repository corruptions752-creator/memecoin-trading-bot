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
