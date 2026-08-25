"""SQLite persistence for positions and fills.

The bot must survive a restart without losing track of open positions, which
on a free host that sleeps is a routine event, not an edge case.
"""

from pathlib import Path
import json
import sqlite3

from .models import ExitReason, Fill, Position, Side

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mint                TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    entry_price_usd     REAL    NOT NULL,
    quantity            REAL    NOT NULL,
    initial_quantity    REAL    NOT NULL,
    cost_usd            REAL    NOT NULL,
    opened_at           REAL    NOT NULL,
    entry_liquidity_usd REAL    NOT NULL DEFAULT 0,
    peak_price_usd      REAL    NOT NULL DEFAULT 0,
    realized_usd        REAL    NOT NULL DEFAULT 0,
    took_first_profit   INTEGER NOT NULL DEFAULT 0,
    unverified_reasons  TEXT    NOT NULL DEFAULT '[]',
    closed_at           REAL,
    close_reason        TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_open
    ON positions (closed_at) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS fills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id   INTEGER REFERENCES positions (id),
    mint          TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    price_usd     REAL    NOT NULL,
    quantity      REAL    NOT NULL,
    gross_usd     REAL    NOT NULL,
    fee_usd       REAL    NOT NULL,
    slippage_usd  REAL    NOT NULL,
    at            REAL    NOT NULL,
    reason        TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_fills_position ON fills (position_id);
CREATE INDEX IF NOT EXISTS idx_fills_at ON fills (at);

CREATE TABLE IF NOT EXISTS risk_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    cash_usd            REAL    NOT NULL,
    open_cost_usd       REAL    NOT NULL,
    realized_today_usd  REAL    NOT NULL,
    current_day         TEXT    NOT NULL,
    halted              INTEGER NOT NULL DEFAULT 0,
    halt_reason         TEXT    NOT NULL DEFAULT '',
    updated_at          REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    at             REAL    NOT NULL,
    scanned        INTEGER NOT NULL,
    rejected       INTEGER NOT NULL,
    candidates     INTEGER NOT NULL,
    shortlisted    INTEGER NOT NULL DEFAULT 0,
    rpc_failures   INTEGER NOT NULL DEFAULT 0,
    rpc_lookups    INTEGER NOT NULL DEFAULT 0,
    near_misses    TEXT    NOT NULL DEFAULT '[]',
    entered        INTEGER NOT NULL,
    rejections     TEXT    NOT NULL DEFAULT '{}',
    skipped_reason TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS activity_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         REAL    NOT NULL,
    scanned    INTEGER NOT NULL,
    candidates INTEGER NOT NULL,
    entered    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_history_at ON activity_history (at);

CREATE TABLE IF NOT EXISTS scan_tokens (
    mint       TEXT    PRIMARY KEY,
    at         REAL    NOT NULL,
    assessment TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scan_tokens_at ON scan_tokens (at);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      REAL    NOT NULL,
    kind    TEXT    NOT NULL,
    symbol  TEXT    NOT NULL DEFAULT '',
    mint    TEXT    NOT NULL DEFAULT '',
    message TEXT    NOT NULL,
    detail  TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_at ON events (at);

CREATE TABLE IF NOT EXISTS mint_blocks (
    mint          TEXT    PRIMARY KEY,
    blocked_until REAL    NOT NULL,
    permanent     INTEGER NOT NULL DEFAULT 0,
    reason        TEXT    NOT NULL DEFAULT '',
    created_at    REAL    NOT NULL
);
"""


def _json_column(row: sqlite3.Row, name: str, default):
    """Read a JSON column that an older database may not have."""

    try:
        raw = row[name]
    except (IndexError, KeyError):
        return default
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _column(row: sqlite3.Row, name: str, default: int = 0) -> int:
    """Read a column that an older database may not have.

    Belt and braces alongside the migration: a dashboard read should degrade
    to a zero rather than crash the trading run that was about to publish it.
    """

    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


class Store:
    """Durable record of what the bot holds and what it has traded."""

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(_SCHEMA)
        self._migrate()
        self._connection.commit()

    # Columns added after the first release. CREATE TABLE IF NOT EXISTS does
    # nothing to a table that already exists, so a database written by an
    # older version keeps its old shape and every read of a new column raises.
    # That is not hypothetical: it crashed five consecutive scheduled runs.
    _MIGRATIONS = (
        ("activity", "shortlisted", "INTEGER NOT NULL DEFAULT 0"),
        ("activity", "rpc_failures", "INTEGER NOT NULL DEFAULT 0"),
        ("activity", "rpc_lookups", "INTEGER NOT NULL DEFAULT 0"),
        ("activity", "near_misses", "TEXT NOT NULL DEFAULT '[]'"),
        ("positions", "unverified_reasons", "TEXT NOT NULL DEFAULT '[]'"),
    )

    def _migrate(self) -> None:
        """Add any columns this version expects but an older file lacks."""

        for table, column, definition in self._MIGRATIONS:
            existing = {
                row["name"] for row in
                self._connection.execute(f"PRAGMA table_info({table})")
            }
            if not existing:
                continue  # table not created yet; the schema will handle it
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )

    def close(self) -> None:
        """Close the underlying connection."""

        self._connection.close()

    # --- Positions --------------------------------------------------------

    def open_position(self, position: Position) -> Position:
        """Persist a new position and return it with its assigned id."""

        cursor = self._connection.execute(
            """
            INSERT INTO positions (
                mint, symbol, entry_price_usd, quantity, initial_quantity,
                cost_usd, opened_at, entry_liquidity_usd, peak_price_usd,
                realized_usd, took_first_profit, unverified_reasons
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position.mint,
                position.symbol,
                position.entry_price_usd,
                position.quantity,
                position.initial_quantity,
                position.cost_usd,
                position.opened_at,
                position.entry_liquidity_usd,
                position.peak_price_usd,
                position.realized_usd,
                int(position.took_first_profit),
                json.dumps(list(position.unverified_reasons)),
            ),
        )
        self._connection.commit()
        position.position_id = int(cursor.lastrowid)
        return position

    def update_position(self, position: Position) -> None:
        """Write back the mutable fields of an open position."""

        if position.position_id is None:
            raise ValueError("cannot update a position that was never stored")
        self._connection.execute(
            """
            UPDATE positions
               SET quantity = ?, peak_price_usd = ?, realized_usd = ?,
                   took_first_profit = ?
             WHERE id = ?
            """,
            (
                position.quantity,
                position.peak_price_usd,
                position.realized_usd,
                int(position.took_first_profit),
                position.position_id,
            ),
        )
        self._connection.commit()

    def close_position(
        self, position: Position, reason: ExitReason, at: float
    ) -> None:
        """Mark a position closed."""

        if position.position_id is None:
            raise ValueError("cannot close a position that was never stored")
        self._connection.execute(
            """
            UPDATE positions
               SET quantity = ?, realized_usd = ?, peak_price_usd = ?,
                   took_first_profit = ?, closed_at = ?, close_reason = ?
             WHERE id = ?
            """,
            (
                position.quantity,
                position.realized_usd,
                position.peak_price_usd,
                int(position.took_first_profit),
                at,
                reason.value,
                position.position_id,
            ),
        )
        self._connection.commit()

    def load_open_positions(self) -> list[Position]:
        """Rehydrate every position that has not been closed."""

        rows = self._connection.execute(
            "SELECT * FROM positions WHERE closed_at IS NULL ORDER BY id"
        ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def closed_positions(self) -> list[sqlite3.Row]:
        """Return closed position rows, oldest first, for reporting."""

        return self._connection.execute(
            "SELECT * FROM positions WHERE closed_at IS NOT NULL "
            "ORDER BY closed_at"
        ).fetchall()

    # --- Fills ------------------------------------------------------------

    def record_fill(self, fill: Fill, position_id: int | None) -> None:
        """Append a fill to the trade log."""

        self._connection.execute(
            """
            INSERT INTO fills (
                position_id, mint, symbol, side, price_usd, quantity,
                gross_usd, fee_usd, slippage_usd, at, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position_id,
                fill.mint,
                fill.symbol,
                fill.side.value,
                fill.price_usd,
                fill.quantity,
                fill.gross_usd,
                fill.fee_usd,
                fill.slippage_usd,
                fill.at,
                fill.reason,
            ),
        )
        self._connection.commit()

    def all_fills(self) -> list[Fill]:
        """Return every recorded fill, oldest first."""

        rows = self._connection.execute(
            "SELECT * FROM fills ORDER BY at, id"
        ).fetchall()
        return [
            Fill(
                mint=row["mint"],
                symbol=row["symbol"],
                side=Side(row["side"]),
                price_usd=row["price_usd"],
                quantity=row["quantity"],
                gross_usd=row["gross_usd"],
                fee_usd=row["fee_usd"],
                slippage_usd=row["slippage_usd"],
                at=row["at"],
                reason=row["reason"],
            )
            for row in rows
        ]

    # --- Scan activity ----------------------------------------------------

    def save_activity(
        self, *, at: float, scanned: int, rejected: int, candidates: int,
        entered: int, rejections: dict, skipped_reason: str = "",
        shortlisted: int = 0, rpc_failures: int = 0, rpc_lookups: int = 0,
        near_misses: list | None = None,
    ) -> None:
        """Record the most recent scan, overwriting the previous one."""

        self._connection.execute(
            """
            INSERT INTO activity (
                id, at, scanned, rejected, candidates, shortlisted,
                rpc_failures, rpc_lookups, entered, rejections,
                skipped_reason, near_misses
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                at = excluded.at, scanned = excluded.scanned,
                rejected = excluded.rejected, candidates = excluded.candidates,
                shortlisted = excluded.shortlisted,
                rpc_failures = excluded.rpc_failures,
                rpc_lookups = excluded.rpc_lookups,
                near_misses = excluded.near_misses,
                entered = excluded.entered, rejections = excluded.rejections,
                skipped_reason = excluded.skipped_reason
            """,
            (
                at, scanned, rejected, candidates, shortlisted,
                rpc_failures, rpc_lookups, entered,
                json.dumps(rejections), skipped_reason,
                json.dumps(near_misses or []),
            ),
        )
        self._connection.execute(
            "INSERT INTO activity_history (at, scanned, candidates, entered) "
            "VALUES (?, ?, ?, ?)",
            (at, scanned, candidates, entered),
        )
        # Keep the history bounded; the page only plots the recent tail.
        self._connection.execute(
            "DELETE FROM activity_history WHERE id NOT IN "
            "(SELECT id FROM activity_history ORDER BY at DESC LIMIT 200)"
        )
        self._connection.commit()

    def activity_history(self, limit: int = 60) -> list[dict]:
        """Recent scans, oldest first, for the dashboard's trend line."""

        rows = self._connection.execute(
            "SELECT at, scanned, candidates, entered FROM activity_history "
            "ORDER BY at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "at": r["at"], "scanned": r["scanned"],
                "candidates": r["candidates"], "entered": r["entered"],
            }
            for r in reversed(rows)
        ]

    def first_activity_at(self) -> float | None:
        """When the first recorded scan happened, or ``None``.

        The history is capped, so this is the start of the retained window
        rather than of all time -- close enough to date a run, and it never
        pretends to more precision than it has.
        """

        row = self._connection.execute(
            "SELECT MIN(at) AS first FROM activity_history"
        ).fetchone()
        return row["first"] if row and row["first"] else None

    def activity_totals(self) -> tuple[int, int]:
        """Retained cycle count and total tokens seen across them."""

        row = self._connection.execute(
            "SELECT COUNT(*) AS cycles, COALESCE(SUM(scanned), 0) AS seen "
            "FROM activity_history"
        ).fetchone()
        return (int(row["cycles"]), int(row["seen"])) if row else (0, 0)

    def load_activity(self) -> dict | None:
        """The most recent scan, or ``None`` before the first one."""

        row = self._connection.execute(
            "SELECT * FROM activity WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        try:
            rejections = json.loads(row["rejections"])
        except (TypeError, ValueError):
            rejections = {}
        return {
            "at": row["at"], "scanned": row["scanned"],
            "rejected": row["rejected"], "candidates": row["candidates"],
            "shortlisted": _column(row, "shortlisted"),
            "rpc_failures": _column(row, "rpc_failures"),
            "rpc_lookups": _column(row, "rpc_lookups"),
            "near_misses": _json_column(row, "near_misses", []),
            "entered": row["entered"], "rejections": rejections,
            "skipped_reason": row["skipped_reason"],
        }

    # --- Scanner snapshots ------------------------------------------------

    def save_scan_tokens(self, assessments: list[dict], at: float) -> None:
        """Replace the scanner's view with this cycle's assessments.

        Replaced rather than accumulated on purpose. A token card carries a
        live price and a live risk read; keeping a token that dropped out of
        the feed would leave the terminal showing an hour-old price as if it
        were current, which is worse than showing nothing.
        """

        self._connection.execute("DELETE FROM scan_tokens")
        self._connection.executemany(
            "INSERT INTO scan_tokens (mint, at, assessment) VALUES (?, ?, ?)",
            [
                (a.get("mint", ""), at, json.dumps(a))
                for a in assessments if a.get("mint")
            ],
        )
        self._connection.commit()

    def scan_tokens(self) -> list[dict]:
        """This cycle's assessments, highest confidence first."""

        rows = self._connection.execute(
            "SELECT assessment FROM scan_tokens"
        ).fetchall()
        out = []
        for row in rows:
            try:
                out.append(json.loads(row["assessment"]))
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda a: a.get("confidence", 0.0), reverse=True)
        return out

    # --- Event log --------------------------------------------------------

    _EVENT_LIMIT = 400
    _HEARTBEAT_LIMIT = 40

    def record_events(self, events: list[dict]) -> None:
        """Append events to the log and trim it to the retained window."""

        if not events:
            return
        self._connection.executemany(
            "INSERT INTO events (at, kind, symbol, mint, message, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    float(e.get("at", 0.0)), str(e.get("kind", "info")),
                    str(e.get("symbol", "")), str(e.get("mint", "")),
                    str(e.get("message", "")),
                    json.dumps(e.get("detail", {})),
                )
                for e in events
            ],
        )
        # Heartbeats are trimmed harder than trades. A cycle emits a scan
        # line every time, so a single flat cap would push the buys and
        # sells -- the events actually worth keeping -- out of the log
        # within a couple of hours.
        self._connection.execute(
            "DELETE FROM events WHERE kind = 'scan' AND id NOT IN "
            "(SELECT id FROM events WHERE kind = 'scan' "
            " ORDER BY id DESC LIMIT ?)",
            (self._HEARTBEAT_LIMIT,),
        )
        self._connection.execute(
            "DELETE FROM events WHERE id NOT IN "
            "(SELECT id FROM events ORDER BY id DESC LIMIT ?)",
            (self._EVENT_LIMIT,),
        )
        self._connection.commit()

    def record_event(
        self, kind: str, message: str, at: float, *, symbol: str = "",
        mint: str = "", detail: dict | None = None,
    ) -> None:
        """Append a single event."""

        self.record_events([{
            "at": at, "kind": kind, "symbol": symbol, "mint": mint,
            "message": message, "detail": detail or {},
        }])

    def recent_events(self, limit: int = 80) -> list[dict]:
        """The newest events first, for the activity feed."""

        rows = self._connection.execute(
            "SELECT at, kind, symbol, mint, message, detail FROM events "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "at": row["at"], "kind": row["kind"], "symbol": row["symbol"],
                "mint": row["mint"], "message": row["message"],
                "detail": _json_column(row, "detail", {}),
            }
            for row in rows
        ]

    # --- Risk state -------------------------------------------------------

    def save_risk_state(
        self, *, cash_usd: float, open_cost_usd: float,
        realized_today_usd: float, current_day: str, halted: bool,
        halt_reason: str, at: float,
    ) -> None:
        """Persist the bankroll and circuit-breaker state.

        Without this a restart re-reads the starting bankroll while also
        reloading open positions, inventing capital on every restart and
        silently clearing a loss halt.
        """

        self._connection.execute(
            """
            INSERT INTO risk_state (
                id, cash_usd, open_cost_usd, realized_today_usd,
                current_day, halted, halt_reason, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                cash_usd           = excluded.cash_usd,
                open_cost_usd      = excluded.open_cost_usd,
                realized_today_usd = excluded.realized_today_usd,
                current_day        = excluded.current_day,
                halted             = excluded.halted,
                halt_reason        = excluded.halt_reason,
                updated_at         = excluded.updated_at
            """,
            (
                cash_usd, open_cost_usd, realized_today_usd, current_day,
                int(halted), halt_reason, at,
            ),
        )
        self._connection.commit()

    def load_risk_state(self) -> dict | None:
        """Return the saved risk state, or ``None`` on a first run."""

        row = self._connection.execute(
            "SELECT * FROM risk_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "cash_usd": row["cash_usd"],
            "open_cost_usd": row["open_cost_usd"],
            "realized_today_usd": row["realized_today_usd"],
            "current_day": row["current_day"],
            "halted": bool(row["halted"]),
            "halt_reason": row["halt_reason"],
        }

    # --- Re-entry blocklist ----------------------------------------------

    def block_mint(
        self, mint: str, blocked_until: float, reason: str, *,
        permanent: bool = False, at: float = 0.0,
    ) -> None:
        """Bar a mint from being re-entered until ``blocked_until``.

        An existing block is only ever extended, never shortened, so a
        permanent rug ban cannot be downgraded by a later ordinary exit.
        """

        existing = self._connection.execute(
            "SELECT blocked_until, permanent FROM mint_blocks WHERE mint = ?",
            (mint,),
        ).fetchone()
        if existing is not None:
            if existing["permanent"]:
                return
            blocked_until = max(blocked_until, existing["blocked_until"])

        self._connection.execute(
            """
            INSERT INTO mint_blocks (mint, blocked_until, permanent, reason,
                                     created_at)
                 VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (mint) DO UPDATE SET
                blocked_until = excluded.blocked_until,
                permanent     = excluded.permanent,
                reason        = excluded.reason
            """,
            (mint, blocked_until, int(permanent), reason, at or blocked_until),
        )
        self._connection.commit()

    def blocked_mints(self, at: float) -> dict[str, str]:
        """Mints that may not be bought right now, mapped to the reason."""

        rows = self._connection.execute(
            "SELECT mint, reason FROM mint_blocks "
            "WHERE permanent = 1 OR blocked_until > ?",
            (at,),
        ).fetchall()
        return {row["mint"]: row["reason"] for row in rows}

    def is_blocked(self, mint: str, at: float) -> bool:
        """Whether ``mint`` is currently barred from re-entry."""

        row = self._connection.execute(
            "SELECT 1 FROM mint_blocks WHERE mint = ? "
            "AND (permanent = 1 OR blocked_until > ?)",
            (mint, at),
        ).fetchone()
        return row is not None

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Position:
        """Build a :class:`Position` from a database row."""

        return Position(
            mint=row["mint"],
            symbol=row["symbol"],
            entry_price_usd=row["entry_price_usd"],
            quantity=row["quantity"],
            cost_usd=row["cost_usd"],
            opened_at=row["opened_at"],
            entry_liquidity_usd=row["entry_liquidity_usd"],
            peak_price_usd=row["peak_price_usd"],
            realized_usd=row["realized_usd"],
            took_first_profit=bool(row["took_first_profit"]),
            initial_quantity=row["initial_quantity"],
            unverified_reasons=tuple(
                _json_column(row, "unverified_reasons", [])
            ),
            position_id=row["id"],
        )
