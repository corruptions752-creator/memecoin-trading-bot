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

    positions = []
    for position in store.load_open_positions():
        positions.append({
            "symbol": position.symbol,
            "mint": position.mint,
            "quantity": position.quantity,
            "entry_price": position.entry_price_usd,
            "cost_usd": position.cost_usd,
            "peak_price": position.peak_price_usd,
            "took_profit": position.took_first_profit,
            "age_hours": (now - position.opened_at) / 3_600.0,
            "stop_price": position.entry_price_usd * (1 - settings.stop_loss_pct),
            "target_price": (
                position.entry_price_usd * settings.take_profit_multiple
            ),
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

    return {
        "mode": settings.mode,
        "activity": activity,
        "scan_history": history,
        "run": {
            "started_at": first,
            "days": (now - first) / 86_400.0 if first else 0.0,
            "cycles": cycles,
            "tokens_seen": tokens_seen,
        },
        "lp_policy": resolve_lp_policy(settings),
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
