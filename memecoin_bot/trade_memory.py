"""The record of what every trade looked like when it was opened.

The ledger stores what happened; this stores the conditions it happened
under, which is what makes a past trade comparable to a present candidate.
Without it the historical agent can only say "we lost money before", never
"this resembles 73 earlier setups".

Features are recorded at entry and never rewritten. Editing a past setup
after seeing its outcome is the purest form of the lookahead this system
is built to avoid.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_features (
    position_id      INTEGER PRIMARY KEY,
    mint             TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    strategy         TEXT    NOT NULL,
    regime           TEXT    NOT NULL DEFAULT 'unknown',
    opened_at        REAL    NOT NULL,
    momentum_5m      REAL    NOT NULL DEFAULT 0,
    momentum_1h      REAL    NOT NULL DEFAULT 0,
    momentum_24h     REAL    NOT NULL DEFAULT 0,
    turnover         REAL    NOT NULL DEFAULT 0,
    buy_sell_ratio   REAL    NOT NULL DEFAULT 0,
    liquidity_usd    REAL    NOT NULL DEFAULT 0,
    volume_24h_usd   REAL    NOT NULL DEFAULT 0,
    fdv_usd          REAL    NOT NULL DEFAULT 0,
    pair_age_hours   REAL    NOT NULL DEFAULT 0,
    entry_confidence REAL    NOT NULL DEFAULT 0,
    reports          TEXT    NOT NULL DEFAULT '{}',
    -- Filled in at exit. Null while the trade is open.
    closed_at        REAL,
    held_hours       REAL,
    max_favourable   REAL,
    max_adverse      REAL,
    exit_reason      TEXT,
    realized_usd     REAL,
    win              INTEGER
);
CREATE INDEX IF NOT EXISTS idx_features_closed
    ON trade_features (closed_at);
"""


@dataclass(frozen=True)
class SetupFeatures:
    """The measurable conditions at the moment of entry."""

    momentum_5m: float = 0.0
    momentum_1h: float = 0.0
    momentum_24h: float = 0.0
    turnover: float = 0.0
    buy_sell_ratio: float = 0.0
    liquidity_usd: float = 0.0
    volume_24h_usd: float = 0.0
    fdv_usd: float = 0.0
    pair_age_hours: float = 0.0

    @classmethod
    def from_snapshot(cls, snapshot) -> "SetupFeatures":
        return cls(
            momentum_5m=snapshot.price_change_5m,
            momentum_1h=snapshot.price_change_1h,
            momentum_24h=snapshot.price_change_24h,
            turnover=snapshot.volume_to_liquidity_24h,
            buy_sell_ratio=snapshot.buy_sell_ratio_5m,
            liquidity_usd=snapshot.liquidity_usd,
            volume_24h_usd=snapshot.volume_24h_usd,
            fdv_usd=snapshot.fdv_usd,
            pair_age_hours=snapshot.age_seconds / 3_600.0,
        )


# Scales that turn a raw difference into "how different is this, really".
# A 5% move in momentum matters; a $5,000 difference in a $200,000 pool
# does not, so each dimension is divided by a plausible spread before the
# distances are combined.
_SCALES = {
    "momentum_5m": 0.15,
    "momentum_1h": 0.40,
    "momentum_24h": 1.50,
    "turnover": 5.0,
    "buy_sell_ratio": 1.0,
    "liquidity_usd": 60_000.0,
    "volume_24h_usd": 500_000.0,
    "pair_age_hours": 48.0,
}


def distance(left: SetupFeatures, right: "SetupFeatures | dict") -> float:
    """Normalised distance between two setups. 0 is identical.

    Plain scaled Euclidean over the dimensions above. Deliberately simple:
    a cleverer metric fitted to 100 trades would be fitting noise, and the
    honest use of this number is "near or not near", not a ranking to three
    decimal places.
    """

    other = right if isinstance(right, dict) else asdict(right)
    mine = asdict(left)
    total = 0.0
    for key, scale in _SCALES.items():
        gap = (mine.get(key, 0.0) or 0.0) - (other.get(key, 0.0) or 0.0)
        total += (gap / scale) ** 2
    return total ** 0.5


class TradeMemory:
    """Reads and writes setup records alongside the trading ledger."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def record_entry(
        self, *, position_id: int, mint: str, symbol: str, strategy: str,
        regime: str, opened_at: float, features: SetupFeatures,
        entry_confidence: float = 0.0, reports: dict | None = None,
    ) -> None:
        self._connection.execute(
            """
            INSERT OR REPLACE INTO trade_features (
                position_id, mint, symbol, strategy, regime, opened_at,
                momentum_5m, momentum_1h, momentum_24h, turnover,
                buy_sell_ratio, liquidity_usd, volume_24h_usd, fdv_usd,
                pair_age_hours, entry_confidence, reports
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                position_id, mint, symbol, strategy, regime, opened_at,
                features.momentum_5m, features.momentum_1h,
                features.momentum_24h, features.turnover,
                features.buy_sell_ratio, features.liquidity_usd,
                features.volume_24h_usd, features.fdv_usd,
                features.pair_age_hours, entry_confidence,
                json.dumps(reports or {}),
            ),
        )
        self._connection.commit()

    def record_exit(
        self, *, position_id: int, closed_at: float, held_hours: float,
        max_favourable: float, max_adverse: float, exit_reason: str,
        realized_usd: float,
    ) -> None:
        self._connection.execute(
            """
            UPDATE trade_features
               SET closed_at = ?, held_hours = ?, max_favourable = ?,
                   max_adverse = ?, exit_reason = ?, realized_usd = ?,
                   win = ?
             WHERE position_id = ?
            """,
            (closed_at, held_hours, max_favourable, max_adverse, exit_reason,
             realized_usd, 1 if realized_usd > 0 else 0, position_id),
        )
        self._connection.commit()

    def closed(self, *, regime: str | None = None) -> list[sqlite3.Row]:
        """Completed setups, oldest first."""

        sql = "SELECT * FROM trade_features WHERE closed_at IS NOT NULL"
        params: tuple = ()
        if regime:
            sql += " AND regime = ?"
            params = (regime,)
        return self._connection.execute(sql + " ORDER BY closed_at", params).fetchall()

    def neighbours(
        self, features: SetupFeatures, *, radius: float = 1.5,
        regime: str | None = None,
    ) -> list[sqlite3.Row]:
        """Closed setups within ``radius`` of this one."""

        return [
            row for row in self.closed(regime=regime)
            if distance(features, dict(row)) <= radius
        ]


def agent_accuracy(rows: "list") -> dict[str, dict[str, float]]:
    """How often each agent's call matched the outcome, scored per direction.

    Every closed setup carries the panel's stances at entry, so this scores
    predictions against results with no re-simulation and no hindsight. An
    agent is credited only where it committed: neutral and
    insufficient-data reports are not predictions and are not scored.

    Bullish and bearish calls are kept apart, because they are graded
    against different baselines. When 24% of trades win, a permanently
    bearish agent is right 76% of the time having said nothing at all.
    Scoring both directions against one overall base rate makes that agent
    look like the best forecaster on the panel -- it did here, on the first
    run of this function. `lift` is the number to read: accuracy minus what
    that direction would score by default.
    """

    import json as _json

    wins = sum(1 for r in rows if r["win"])
    total = sum(1 for r in rows if r["win"] is not None)
    win_rate = wins / total if total else 0.0
    baseline = {"bullish": win_rate, "bearish": 1.0 - win_rate}

    tally: dict[str, dict[str, float]] = {}
    for row in rows:
        try:
            reports = _json.loads(row["reports"] or "{}")
        except (ValueError, TypeError):
            continue
        won = bool(row["win"])
        for name, value in reports.items():
            if not isinstance(value, (list, tuple)) or len(value) < 2:
                continue
            stance = value[0]
            if stance not in ("bullish", "bearish"):
                continue
            stats = tally.setdefault(name, {
                "calls": 0.0, "correct": 0.0, "accuracy": 0.0,
                "bull_calls": 0.0, "bull_correct": 0.0,
                "bear_calls": 0.0, "bear_correct": 0.0, "lift": 0.0,
            })
            stats["calls"] += 1
            side = "bull" if stance == "bullish" else "bear"
            stats[f"{side}_calls"] += 1
            if (stance == "bullish") == won:
                stats["correct"] += 1
                stats[f"{side}_correct"] += 1

    for stats in tally.values():
        stats["accuracy"] = (
            stats["correct"] / stats["calls"] if stats["calls"] else 0.0
        )
        # Lift is measured per direction and then weighted by how often the
        # agent actually used each, so an agent cannot earn credit simply
        # for picking the commoner outcome.
        earned = expected = 0.0
        for side, key in (("bull", "bullish"), ("bear", "bearish")):
            calls = stats[f"{side}_calls"]
            if calls:
                earned += stats[f"{side}_correct"]
                expected += calls * baseline[key]
        stats["lift"] = (
            (earned - expected) / stats["calls"] if stats["calls"] else 0.0
        )
    return tally


def base_rate(rows: "list") -> float:
    """Fraction of all closed setups that won.

    An agent's accuracy means nothing without it: 60% correct is skill when
    40% of trades win and noise when 60% do.
    """

    scored = [r for r in rows if r["win"] is not None]
    return (sum(1 for r in scored if r["win"]) / len(scored)) if scored else 0.0
