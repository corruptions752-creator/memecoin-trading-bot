"""A small web dashboard for watching the bot trade.

Serves one self-contained page plus a JSON endpoint, both from the standard
library, so watching the bot costs no dependencies and no external service.
The page polls the endpoint and re-renders, which is enough for a loop that
ticks every thirty seconds.

Everything shown is read from the same SQLite file the trading loop writes to,
so the dashboard cannot show anything the bot did not actually do.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import logging
import threading
import time

from .config import Settings, resolve_lp_policy
from .reporting import summarize
from .store import Store

log = logging.getLogger(__name__)

_PAGE = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


def build_state(settings: Settings, store: Store) -> dict:
    """Collect everything the page renders, straight from the database."""

    now = time.time()
    risk = store.load_risk_state() or {
        "cash_usd": settings.starting_bankroll_usd,
        "open_cost_usd": 0.0,
        "realized_today_usd": 0.0,
        "current_day": "",
        "halted": False,
        "halt_reason": "",
    }

    # The scanner writes a live price for every token it looked at this
    # cycle, held ones included, so open positions can be marked to market
    # without the dashboard fetching anything of its own.
    scanner = store.scan_tokens() if hasattr(store, "scan_tokens") else []
    marks = {token["mint"]: token for token in scanner if token.get("mint")}

    positions = []
    marked_value = 0.0
    for position in store.load_open_positions():
        mark = marks.get(position.mint)
        price = mark.get("price") if mark else None
        # An unpriced position is carried at cost and says so, rather than
        # being marked at a stale price or silently dropped from equity.
        value = position.quantity * price if price else position.cost_usd
        marked_value += value
        positions.append({
            "symbol": position.symbol,
            "mint": position.mint,
            "quantity": position.quantity,
            "entry_price": position.entry_price_usd,
            "cost_usd": position.cost_usd,
            "peak_price": position.peak_price_usd,
            "took_profit": position.took_first_profit,
            "age_hours": (now - position.opened_at) / 3_600.0,
            "unverified": list(position.unverified_reasons),
            "stop_price": position.entry_price_usd * (1 - settings.stop_loss_pct),
            "target_price": (
                position.entry_price_usd * settings.take_profit_multiple
            ),
            "price": price,
            "priced": price is not None,
            "value_usd": value,
            "unrealized_usd": value - position.cost_usd,
            "unrealized_pct": (
                (value - position.cost_usd) / position.cost_usd
                if position.cost_usd else 0.0
            ),
            "realized_usd": position.realized_usd,
            "change_24h": mark.get("change_24h") if mark else None,
            "risk_band": mark.get("risk_band") if mark else None,
            "risk_score": mark.get("risk_score") if mark else None,
        })

    performance = summarize(store, settings.starting_bankroll_usd)

    # How far into the run we are. Over a multi-week paper test, most cycles
    # do nothing, so a sense of accumulated progress is the difference
    # between "working" and "stuck".
    first = store.first_activity_at()
    cycles, tokens_seen = store.activity_totals()

    # Equity curve: starting capital plus cumulative realized P&L, in close
    # order. This is the same series the drawdown figure is measured on.
    equity, running = [settings.starting_bankroll_usd], settings.starting_bankroll_usd
    closed = []
    for row in store.closed_positions():
        running += row["realized_usd"]
        equity.append(running)
        closed.append({
            "symbol": row["symbol"],
            "realized_usd": row["realized_usd"],
            "reason": row["close_reason"] or "unknown",
            "closed_at": row["closed_at"],
        })

    fills = store.all_fills()
    recent = [
        {
            "symbol": fill.symbol,
            "side": fill.side.value,
            "price": fill.price_usd,
            "usd": fill.gross_usd,
            "fee": fill.fee_usd,
            "reason": fill.reason,
            "at": fill.at,
        }
        for fill in fills[-40:]
    ][::-1]

    activity = store.load_activity()
    history = store.activity_history()
    events = store.recent_events() if hasattr(store, "recent_events") else []

    equity_usd = risk["cash_usd"] + marked_value
    return {
        "mode": settings.mode,
        "activity": activity,
        "scan_history": history,
        "scanner": scanner,
        "events": events,
        "equity_usd": equity_usd,
        "marked_value": marked_value,
        "unrealized_usd": marked_value - risk["open_cost_usd"],
        "risk_monitor": _risk_monitor(
            settings, risk, positions, equity_usd, performance
        ),
        "run": {
            "started_at": first,
            "days": (now - first) / 86_400.0 if first else 0.0,
            "cycles": cycles,
            "tokens_seen": tokens_seen,
        },
        "lp_policy": resolve_lp_policy(settings),
        "profile": settings.profile,
        "verification": getattr(settings, "verification", "strict"),
        "risk": {
            "per_trade_pct": settings.risk_fraction_per_trade,
            "stop_pct": settings.stop_loss_pct,
            "target_multiple": settings.take_profit_multiple,
            "min_entry_score": settings.min_entry_score,
        },
        "generated_at": now,
        "bankroll": risk["cash_usd"] + risk["open_cost_usd"],
        "cash": risk["cash_usd"],
        "open_cost": risk["open_cost_usd"],
        "starting_bankroll": settings.starting_bankroll_usd,
        "realized_today": risk["realized_today_usd"],
        "halted": risk["halted"],
        "halt_reason": risk["halt_reason"],
        "daily_loss_limit": (
            (risk["cash_usd"] + risk["open_cost_usd"])
            * settings.daily_loss_limit_pct
        ),
        "max_positions": settings.max_open_positions,
        "positions": positions,
        "equity": equity,
        "closed": closed[-30:][::-1],
        "recent_fills": recent,
        "stats": {
            "trades": performance.trades,
            "wins": performance.wins,
            "losses": performance.losses,
            "win_rate": performance.win_rate,
            "realized_usd": performance.realized_usd,
            "expectancy_usd": performance.expectancy_usd,
            "max_drawdown": performance.max_drawdown,
            "profit_factor": (
                performance.profit_factor
                if performance.profit_factor != float("inf") else None
            ),
            "fees_usd": performance.total_fees_usd,
            "slippage_usd": performance.total_slippage_usd,
            "loss_streak": performance.largest_loss_streak,
            "exits": performance.exit_breakdown,
            "pnl_by_exit": performance.pnl_by_exit,
        },
    }


def _risk_monitor(
    settings: Settings, risk: dict, positions: list, equity: float,
    performance,
) -> dict:
    """Portfolio-level exposure, each figure measured rather than scored.

    These are the numbers that decide whether a bad day becomes a bad week:
    how much of the bankroll is at risk at once, how much of it sits in a
    single token, how much of today's loss budget is spent, and how much has
    been bought without the contract checks passing.
    """

    exposure = risk["open_cost_usd"] / equity if equity > 0 else 0.0
    largest = max((p["value_usd"] for p in positions), default=0.0)
    concentration = largest / equity if equity > 0 else 0.0

    limit = equity * settings.daily_loss_limit_pct
    lost_today = max(0.0, -risk["realized_today_usd"])
    budget_used = lost_today / limit if limit > 0 else 0.0

    unverified = [p for p in positions if p["unverified"]]
    danger = [p for p in positions if p.get("risk_band") == "DANGER"]
    unpriced = [p for p in positions if not p["priced"]]

    # Worst case if every open position stopped out at once, at the
    # configured stop. Slippage on the way out makes the real number worse.
    stop_risk = sum(p["value_usd"] for p in positions) * settings.stop_loss_pct

    return {
        "exposure_pct": exposure,
        "slots_used": len(positions),
        "slots": settings.max_open_positions,
        "concentration_pct": concentration,
        "daily_budget_used_pct": budget_used,
        "daily_loss_limit_usd": limit,
        "lost_today_usd": lost_today,
        "stop_risk_usd": stop_risk,
        "stop_risk_pct": stop_risk / equity if equity > 0 else 0.0,
        "unverified_count": len(unverified),
        "unverified_symbols": [p["symbol"] for p in unverified],
        "danger_count": len(danger),
        "danger_symbols": [p["symbol"] for p in danger],
        "unpriced_count": len(unpriced),
        "loss_streak": performance.largest_loss_streak,
        "max_drawdown": performance.max_drawdown,
        "halted": risk["halted"],
        "halt_reason": risk["halt_reason"],
    }


class _Handler(BaseHTTPRequestHandler):
    """Serves the page and the state endpoint."""

    settings: Settings = None       # type: ignore[assignment]
    database_path: str = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.startswith("/api/state"):
            self._send_state()
        elif self.path == "/favicon.ico":
            # Answer rather than 404, so the browser console stays clean.
            self._send(204, "image/x-icon", b"")
        elif self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))
        else:
            self._send(404, "text/plain", b"not found")

    def _send_state(self) -> None:
        """Open the database per request so the trading loop keeps its own."""

        try:
            store = Store(self.database_path)
            try:
                payload = build_state(self.settings, store)
            finally:
                store.close()
        except Exception as error:  # noqa: BLE001 - never take the page down
            log.exception("dashboard state failed")
            payload = {"error": str(error)}
        self._send(
            200, "application/json",
            json.dumps(payload).encode("utf-8"),
        )

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Silence per-request logging; the trading log is what matters."""


def default_port() -> int:
    """The port to serve on.

    Hosts like Replit hand the public port in ``PORT``; honouring it is what
    makes the dashboard reachable from a phone rather than only locally.
    """

    import os

    raw = os.getenv("PORT", "").strip()
    if raw.isdigit():
        return int(raw)
    return 8080


def serve(
    settings: Settings, host: str = "0.0.0.0", port: int = 8080,
    background: bool = False,
) -> ThreadingHTTPServer:
    """Start the dashboard server.

    ``background=True`` runs it on a daemon thread so the trading loop can
    own the main thread.
    """

    handler = type("Handler", (_Handler,), {
        "settings": settings,
        "database_path": settings.database_path,
    })
    server = ThreadingHTTPServer((host, port), handler)

    if background:
        thread = threading.Thread(
            target=server.serve_forever, name="dashboard", daemon=True
        )
        thread.start()
        log.info("dashboard on http://%s:%d", host, port)
        return server

    log.info("dashboard on http://%s:%d — Ctrl+C to stop", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("dashboard stopped")
    finally:
        server.server_close()
    return server
