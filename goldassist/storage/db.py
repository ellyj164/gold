"""SQLite persistence.

Holds the four record types the specification calls for: the signal history
(every setup the engine produced), the trading journal (what the trader
actually did), backtest results, and user settings.

Everything is stored in one file under ~/.goldassist so the whole personal
trading database is a single artefact the trader can back up or move.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..config import APP_DIR, DB_PATH
from ..jsonsafe import safe

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    symbol          TEXT NOT NULL DEFAULT 'XAUUSD',
    price           REAL NOT NULL,
    decision        TEXT NOT NULL,          -- BUY | SELL | WAIT
    direction       TEXT NOT NULL,
    score           REAL NOT NULL,
    opposing_score  REAL,
    regime          TEXT,
    profile         TEXT,
    timeframe_reads TEXT,                   -- JSON
    evidence        TEXT,                   -- JSON
    penalties       TEXT,                   -- JSON
    waiting_for     TEXT,                   -- JSON
    invalidation    REAL,
    invalidation_note TEXT,
    targets         TEXT,                   -- JSON
    reward_risk     REAL,
    ai_explanation  TEXT,
    setup_type      TEXT,                   -- pullback | breakout | reversal | ...
    setup_label     TEXT,
    status          TEXT,                   -- lifecycle state at the time of record
    quality         TEXT,                   -- No Setup | Weak | ... | Very Strong
    entry_low       REAL,
    entry_high      REAL,
    entry_preferred REAL,
    entry_rationale TEXT,
    trigger_text    TEXT,
    trigger_met     INTEGER,
    suggestion      TEXT,                   -- JSON: the full signal card
    result          TEXT,                   -- win | loss | breakeven | untaken | open
    result_pips     REAL,
    resolved_at     TEXT,
    snapshot        TEXT                    -- JSON: the full decision object
);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_decision ON signals(decision);

CREATE TABLE IF NOT EXISTS journal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER REFERENCES signals(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL,
    took_trade      INTEGER NOT NULL,       -- 0/1
    reason          TEXT,                   -- why taken, or why rejected
    entry           REAL,
    stop_loss       REAL,
    take_profit     REAL,
    lot_size        REAL,
    result          TEXT,                   -- win | loss | breakeven | open
    profit          REAL,
    screenshot_path TEXT,
    notes           TEXT,
    closed_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_created ON journal(created_at DESC);

CREATE TABLE IF NOT EXISTS backtests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    profile         TEXT NOT NULL,
    start_date      TEXT,
    end_date        TEXT,
    timeframe       TEXT,
    stats           TEXT NOT NULL,          -- JSON
    trades          TEXT                    -- JSON
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Every tracked setup, recorded automatically when it terminates. This is the
-- table the learning layer reads back: the signals table records what the
-- engine *said*, this one records what actually happened to it.
CREATE TABLE IF NOT EXISTS setup_outcomes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_uid      TEXT,                   -- the tracked suggestion's id
    created_at      TEXT NOT NULL,          -- when the setup was identified
    closed_at       TEXT NOT NULL,
    direction       TEXT,
    setup_type      TEXT,
    session         TEXT,
    regime          TEXT,
    day_verdict     TEXT,                   -- TRADE | SELECTIVE | STAND ASIDE
    score           REAL,
    quality         TEXT,
    entry           REAL,
    invalidation    REAL,
    tp1             REAL,
    entered         INTEGER NOT NULL DEFAULT 0,
    outcome         TEXT,                   -- win | loss | scratch | untaken
    r_multiple      REAL,
    max_favourable_r REAL,
    max_adverse_r   REAL,
    reason          TEXT,
    notes           TEXT,
    -- which strategy found it, and how it actually closed
    strategy        TEXT,
    strategy_name   TEXT,
    family          TEXT,
    trigger_family  TEXT,
    alignment       TEXT,
    conviction      REAL,
    context_score   REAL,
    confirmations   TEXT,
    fill_price      REAL,
    filled_at       TEXT,
    exit_price      REAL,
    exit_reason     TEXT,                   -- target | stop | breakeven | expiry | ...
    result_pips     REAL,
    targets_hit     INTEGER,
    mfe_pips        REAL,
    mae_pips        REAL,
    opportunity_pips REAL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_closed ON setup_outcomes(closed_at DESC);
CREATE INDEX IF NOT EXISTS idx_outcomes_setup ON setup_outcomes(setup_type);

-- Every signal the strategy library has ever fired, written the moment it is
-- identified rather than when it closes.
--
-- This is the table the Signals tab reads, and it exists because neither of
-- the other two answered the question "what did this system actually call
-- today?". ``signals`` records the top-level decision, and only when that
-- decision *changes* -- so a morning that produced eleven setups from nine
-- strategies could leave a single row saying BUY. ``setup_outcomes`` is
-- complete but is written at termination, so a setup that fired an hour ago
-- and is still running appears nowhere at all.
--
-- Times are stored twice on purpose. ``fired_at`` is UTC and is what any
-- arithmetic uses. ``fired_local``, ``fired_date`` and ``fired_clock`` are the
-- trader's own clock, frozen at the moment of firing, and are what gets read.
-- Storing the display form rather than deriving it later means the log cannot
-- silently re-label its own history if the display offset is ever changed.
CREATE TABLE IF NOT EXISTS fired_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uid             TEXT NOT NULL,          -- the tracked suggestion's id
    fired_at        TEXT NOT NULL,          -- UTC ISO
    fired_local     TEXT,                   -- "2026-08-20 14:37"
    fired_date      TEXT,                   -- "2026-08-20", trader's clock
    fired_clock     TEXT,                   -- "14:37", trader's clock
    clock_offset    TEXT,                   -- "GMT+2"
    session         TEXT,                   -- session key at fire time
    session_label   TEXT,
    session_quality REAL,
    strategy        TEXT,
    strategy_name   TEXT,
    family          TEXT,
    direction       TEXT,
    action          TEXT,                   -- BUY | SELL
    setup_label     TEXT,
    status          TEXT,                   -- status when fired
    score           REAL,
    conviction      REAL,
    context_score   REAL,
    quality         TEXT,
    regime          TEXT,
    price           REAL,                   -- spot when identified
    entry_low       REAL,
    entry_high      REAL,
    entry_preferred REAL,
    stop            REAL,
    tp1             REAL,
    tp2             REAL,
    tp3             REAL,
    reward_risk     REAL,
    first_target_pips REAL,
    max_pips        REAL,
    opportunity     TEXT,                   -- "Good" | "Strong" | "Major"
    trigger_text    TEXT,
    reasons         TEXT,                   -- JSON list
    confirmations   TEXT,                   -- JSON list of agreeing strategies
    -- Filled in later, as the same signal progresses. The row is the signal's
    -- whole life, so the tab can show a fired signal and its result together.
    last_status     TEXT,
    armed_at        TEXT,
    filled_at       TEXT,
    fill_price      REAL,
    closed_at       TEXT,
    closed_local    TEXT,
    outcome         TEXT,                   -- win | loss | scratch | untaken
    exit_price      REAL,
    exit_reason     TEXT,
    result_pips     REAL,
    r_multiple      REAL
);
CREATE INDEX IF NOT EXISTS idx_fired_at ON fired_signals(fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_fired_date ON fired_signals(fired_date);
CREATE INDEX IF NOT EXISTS idx_fired_uid ON fired_signals(uid);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    message         TEXT NOT NULL,
    payload         TEXT,
    seen            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);
"""


class Database:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn) -> None:
        """Add columns introduced after a database was first created.

        SQLite has no 'ADD COLUMN IF NOT EXISTS', so existing columns are read
        first. Keeps an older trading history usable across upgrades.
        """
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(signals)")}
        additions = {
            "setup_type": "TEXT", "setup_label": "TEXT", "status": "TEXT",
            "quality": "TEXT", "entry_low": "REAL", "entry_high": "REAL",
            "entry_preferred": "REAL", "entry_rationale": "TEXT",
            "trigger_text": "TEXT", "trigger_met": "INTEGER", "suggestion": "TEXT",
        }
        for column, kind in additions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {column} {kind}")

        # Outcomes gained the strategy that produced the setup and the detail
        # of how it actually closed, once resolution became bar-accurate.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(setup_outcomes)")}
        outcome_additions = {
            "strategy": "TEXT", "strategy_name": "TEXT", "family": "TEXT",
            "trigger_family": "TEXT", "alignment": "TEXT", "conviction": "REAL",
            "context_score": "REAL", "confirmations": "TEXT",
            "fill_price": "REAL", "filled_at": "TEXT", "exit_price": "REAL",
            "exit_reason": "TEXT", "result_pips": "REAL", "targets_hit": "INTEGER",
            "mfe_pips": "REAL", "mae_pips": "REAL", "opportunity_pips": "REAL",
        }
        for column, kind in outcome_additions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE setup_outcomes ADD COLUMN {column} {kind}")
        # Created here rather than in SCHEMA: on a database that predates the
        # column, the schema script runs before this migration and the index
        # would refer to a column that does not exist yet.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_strategy "
                     "ON setup_outcomes(strategy)")

    def _conn(self) -> sqlite3.Connection:
        # One connection per thread: the API server and the analysis loop run
        # on different threads and SQLite objects are not shareable.
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Release this thread's connection.

        The application never needs this -- it holds one database for its whole
        life -- but anything that creates a throwaway database does, because
        Windows will not delete a file SQLite still has open.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    @contextmanager
    def connect(self):
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # -- signals ------------------------------------------------------------

    def record_signal(self, decision: dict, analysis: dict, explanation: str | None = None,
                      suggestion: dict | None = None) -> int:
        """Store a generated setup. Every decision is recorded, WAIT included.

        ``suggestion`` is the signal card when one is being tracked, so the
        history records the entry zone, trigger and setup type -- not just the
        decision that produced them.
        """
        risk = decision.get("risk") or {}
        inval = decision.get("invalidation") or {}
        card = suggestion or {}
        zone = card.get("entry_zone") or {}
        trigger = card.get("trigger") or {}
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO signals (
                    created_at, price, decision, direction, score, opposing_score,
                    regime, profile, timeframe_reads, evidence, penalties, waiting_for,
                    invalidation, invalidation_note, targets, reward_risk,
                    ai_explanation, result, snapshot,
                    setup_type, setup_label, status, quality,
                    entry_low, entry_high, entry_preferred, entry_rationale,
                    trigger_text, trigger_met, suggestion
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _now(),
                    decision["price"],
                    decision["decision"],
                    decision["direction"],
                    decision["score"],
                    decision.get("opposing_score"),
                    (decision.get("regime") or {}).get("regime"),
                    decision.get("profile"),
                    _j(analysis.get("alignment", {}).get("reads")),
                    _j((decision.get("confluence", {}).get(decision["direction"], {}) or {}).get("evidence")),
                    _j((decision.get("confluence", {}).get(decision["direction"], {}) or {}).get("penalties")),
                    _j(decision.get("waiting_for")),
                    inval.get("price"),
                    inval.get("description") or inval.get("basis"),
                    _j(decision.get("targets")),
                    risk.get("reward_risk"),
                    explanation,
                    "open" if decision["decision"] != "WAIT" else "untaken",
                    _j(decision),
                    card.get("setup_type"),
                    card.get("setup_label"),
                    card.get("status"),
                    card.get("quality"),
                    zone.get("low"),
                    zone.get("high"),
                    zone.get("preferred"),
                    card.get("entry_rationale"),
                    trigger.get("description"),
                    1 if trigger.get("met") else 0,
                    _j(card) if card else None,
                ),
            )
            return int(cur.lastrowid)

    def recent_signals(self, limit: int = 100, decision: str | None = None) -> list[dict]:
        sql = "SELECT * FROM signals"
        params: list = []
        if decision:
            sql += " WHERE decision = ?"
            params.append(decision)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row(r) for r in rows]

    def signal(self, signal_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return _row(row) if row else None

    def resolve_signal(self, signal_id: int, result: str, pips: float | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE signals SET result = ?, result_pips = ?, resolved_at = ? WHERE id = ?",
                (result, pips, _now(), signal_id),
            )

    def last_signal(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM signals ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return _row(row) if row else None

    # -- fired signals ------------------------------------------------------

    #: Columns written when a signal first fires. Kept as a tuple so the insert
    #: and the update below cannot drift apart from the schema.
    FIRED_COLUMNS = (
        "uid", "fired_at", "fired_local", "fired_date", "fired_clock",
        "clock_offset", "session", "session_label", "session_quality",
        "strategy", "strategy_name", "family", "direction", "action",
        "setup_label", "status", "score", "conviction", "context_score",
        "quality", "regime", "price", "entry_low", "entry_high",
        "entry_preferred", "stop", "tp1", "tp2", "tp3", "reward_risk",
        "first_target_pips", "max_pips", "opportunity", "trigger_text",
        "reasons", "confirmations", "last_status",
    )

    def record_fired_signal(self, suggestion: dict) -> int | None:
        """Log a signal at the moment it is identified.

        Idempotent on ``uid``: the book can re-offer the same tracked setup
        across cycles, and a log that grew a row each time would be a log of
        polling rather than of trading.
        """
        uid = suggestion.get("id")
        if not uid:
            return None
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM fired_signals WHERE uid = ?", (uid,)).fetchone()
            if existing:
                return int(existing["id"])
            values = [_fired_value(suggestion, c) for c in self.FIRED_COLUMNS]
            placeholders = ",".join("?" * len(self.FIRED_COLUMNS))
            cur = conn.execute(
                f"INSERT INTO fired_signals ({','.join(self.FIRED_COLUMNS)}) "
                f"VALUES ({placeholders})", values)
            return int(cur.lastrowid)

    def update_fired_signal(self, suggestion: dict) -> None:
        """Carry a signal's progress onto its existing log row.

        Called as a setup arms, fills and finally resolves, so one row tells
        the whole story: fired at 14:37 in the overlap, filled at 14:52,
        stopped at 15:20 for -31 pips.
        """
        uid = suggestion.get("id")
        if not uid:
            return
        closed_at = suggestion.get("exit_at")
        fields = {
            "last_status": suggestion.get("status"),
            "armed_at": suggestion.get("armed_at"),
            "filled_at": suggestion.get("filled_at"),
            "fill_price": suggestion.get("fill_price"),
            "closed_at": closed_at,
            "closed_local": _local_stamp(closed_at),
            "outcome": suggestion.get("outcome"),
            "exit_price": suggestion.get("exit_price"),
            "exit_reason": suggestion.get("exit_reason"),
            "result_pips": suggestion.get("result_pips"),
            "r_multiple": suggestion.get("r_multiple"),
        }
        assignments = ",".join(f"{k} = ?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE fired_signals SET {assignments} WHERE uid = ?",
                         list(fields.values()) + [uid])

    def fired_signals(self, limit: int = 300, date: str | None = None,
                      session: str | None = None, strategy: str | None = None,
                      direction: str | None = None,
                      outcome: str | None = None) -> list[dict]:
        """The signal log, newest first, with the filters the tab offers."""
        clauses, params = [], []
        for column, value in (("fired_date", date), ("session", session),
                              ("strategy", strategy), ("direction", direction)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if outcome == "open":
            # Still running: nothing has closed it yet.
            clauses.append("closed_at IS NULL")
        elif outcome:
            clauses.append("outcome = ?")
            params.append(outcome)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM fired_signals {where} "
                f"ORDER BY fired_at DESC LIMIT ?", params).fetchall()
        return [_row(r) for r in rows]

    def fired_dates(self, limit: int = 60) -> list[dict]:
        """Trading days that have signals, for the tab's date picker."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT fired_date AS date, COUNT(*) AS n "
                "FROM fired_signals WHERE fired_date IS NOT NULL "
                "GROUP BY fired_date ORDER BY fired_date DESC LIMIT ?",
                (limit,)).fetchall()
        return [_row(r) for r in rows]

    def fired_summary(self, date: str | None = None) -> dict:
        """Headline counts for the signal log, optionally for one day."""
        where, params = ("WHERE fired_date = ?", [date]) if date else ("", [])
        with self.connect() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS fired,
                           SUM(CASE WHEN direction = 'bullish' THEN 1 ELSE 0 END) AS buys,
                           SUM(CASE WHEN direction = 'bearish' THEN 1 ELSE 0 END) AS sells,
                           SUM(CASE WHEN filled_at IS NOT NULL THEN 1 ELSE 0 END) AS filled,
                           SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins,
                           SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses,
                           SUM(CASE WHEN closed_at IS NULL THEN 1 ELSE 0 END) AS open,
                           COALESCE(SUM(result_pips), 0) AS pips,
                           AVG(first_target_pips) AS avg_target_pips
                    FROM fired_signals {where}""", params).fetchone()
            by_session = [_row(r) for r in conn.execute(
                f"SELECT session_label AS session, COUNT(*) AS n FROM fired_signals "
                f"{where} GROUP BY session_label ORDER BY n DESC", params).fetchall()]
            by_strategy = [_row(r) for r in conn.execute(
                f"SELECT strategy_name AS strategy, COUNT(*) AS n FROM fired_signals "
                f"{where} GROUP BY strategy_name ORDER BY n DESC", params).fetchall()]
        summary = _row(row)
        decided = (summary.get("wins") or 0) + (summary.get("losses") or 0)
        summary["win_rate"] = (round((summary.get("wins") or 0) / decided * 100, 1)
                               if decided else None)
        summary["by_session"] = by_session
        summary["by_strategy"] = by_strategy
        return summary

    # -- journal ------------------------------------------------------------

    def add_journal_entry(self, entry: dict) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO journal (
                    signal_id, created_at, took_trade, reason, entry, stop_loss,
                    take_profit, lot_size, result, profit, screenshot_path, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    entry.get("signal_id"),
                    _now(),
                    1 if entry.get("took_trade") else 0,
                    entry.get("reason"),
                    entry.get("entry"),
                    entry.get("stop_loss"),
                    entry.get("take_profit"),
                    entry.get("lot_size"),
                    entry.get("result", "open"),
                    entry.get("profit"),
                    entry.get("screenshot_path"),
                    entry.get("notes"),
                ),
            )
            return int(cur.lastrowid)

    def update_journal_entry(self, entry_id: int, fields: dict) -> None:
        allowed = {"result", "profit", "notes", "entry", "stop_loss", "take_profit",
                   "lot_size", "reason", "screenshot_path"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values())
        if updates.get("result") in ("win", "loss", "breakeven"):
            assignments += ", closed_at = ?"
            params.append(_now())
        params.append(entry_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE journal SET {assignments} WHERE id = ?", params)

    def journal_entries(self, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT j.*, s.decision AS signal_decision, s.score AS signal_score,
                          s.direction AS signal_direction, s.price AS signal_price
                   FROM journal j LEFT JOIN signals s ON s.id = j.signal_id
                   ORDER BY j.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [_row(r) for r in rows]

    def journal_stats(self) -> dict:
        """Compare what the engine recommended against what the trader did."""
        entries = self.journal_entries(limit=1000)
        closed = [e for e in entries if e.get("result") in ("win", "loss", "breakeven")]
        taken = [e for e in entries if e.get("took_trade")]
        wins = [e for e in closed if e["result"] == "win"]

        agreed = [e for e in entries
                  if e.get("signal_decision") in ("BUY", "SELL") and e.get("took_trade")]
        overrode = [e for e in entries
                    if e.get("signal_decision") == "WAIT" and e.get("took_trade")]
        skipped = [e for e in entries
                   if e.get("signal_decision") in ("BUY", "SELL") and not e.get("took_trade")]

        return {
            "entries": len(entries),
            "taken": len(taken),
            "closed": len(closed),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "total_profit": round(sum(e.get("profit") or 0 for e in closed), 2),
            "followed_engine": len(agreed),
            "traded_against_wait": len(overrode),
            "skipped_engine_setups": len(skipped),
            "avg_profit_when_following": _avg(
                [e.get("profit") for e in agreed if e.get("profit") is not None]),
            "avg_profit_when_overriding": _avg(
                [e.get("profit") for e in overrode if e.get("profit") is not None]),
        }

    # -- setup outcomes -----------------------------------------------------

    def record_setup_outcome(self, record: dict) -> int:
        """Store how a tracked setup finished.

        Written once per setup, when it reaches a terminal state. Together with
        the signals table this closes the loop: what was suggested, and what it
        was worth.
        """
        columns = (
            "signal_uid", "created_at", "closed_at", "direction", "setup_type",
            "session", "regime", "day_verdict", "score", "quality", "entry",
            "invalidation", "tp1", "entered", "outcome", "r_multiple",
            "max_favourable_r", "max_adverse_r", "reason", "notes",
            "strategy", "strategy_name", "family", "trigger_family", "alignment",
            "conviction", "context_score", "confirmations", "fill_price",
            "filled_at", "exit_price", "exit_reason", "result_pips",
            "targets_hit", "mfe_pips", "mae_pips", "opportunity_pips",
        )
        record = dict(record)
        if not isinstance(record.get("confirmations"), (str, type(None))):
            record["confirmations"] = _j(record["confirmations"])
        values = [record.get(c) for c in columns]
        placeholders = ",".join("?" * len(columns))
        with self.connect() as conn:
            cur = conn.execute(
                f"INSERT INTO setup_outcomes ({','.join(columns)}) "
                f"VALUES ({placeholders})",
                values,
            )
            return int(cur.lastrowid)

    def setup_outcomes(self, limit: int = 500) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM setup_outcomes ORDER BY closed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row(r) for r in rows]

    def strategy_stats(self, limit: int = 1000) -> list[dict]:
        """Per-strategy results, for the interface and the scoring feedback.

        Only setups that actually filled are counted. An idea that expired
        unfilled says nothing about whether the pattern works.

        The test is ``fill_price IS NOT NULL``, not ``entered = 1``. Two rows
        written before the schema migration carry ``entered = 1`` with no fill
        price, no exit price and an r_multiple of +3.5, and they were the only
        reason the recorded total ever read positive. A row without a fill
        price is not a trade, whatever its flags say.
        """
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT strategy,
                       COALESCE(MAX(strategy_name), strategy) AS strategy_name,
                       COALESCE(MAX(family), '')              AS family,
                       COUNT(*)                               AS n,
                       SUM(outcome = 'win')                   AS wins,
                       SUM(outcome = 'loss')                  AS losses,
                       SUM(outcome = 'scratch')               AS scratches,
                       AVG(r_multiple)                        AS avg_r,
                       SUM(r_multiple)                        AS total_r,
                       AVG(result_pips)                       AS avg_pips,
                       SUM(result_pips)                       AS total_pips
                  FROM (SELECT * FROM setup_outcomes
                         WHERE fill_price IS NOT NULL AND strategy IS NOT NULL
                           AND outcome IN ('win', 'loss', 'scratch')
                         ORDER BY closed_at DESC LIMIT ?)
                 GROUP BY strategy
                 ORDER BY n DESC
                """,
                (limit,),
            ).fetchall()
        return [_row(r) for r in rows]

    def setup_outcomes_between(self, start: str, end: str) -> list[dict]:
        """Outcomes closed inside a window -- used for today's risk counters."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM setup_outcomes WHERE closed_at >= ? AND closed_at < ? "
                "ORDER BY closed_at ASC",
                (start, end),
            ).fetchall()
        return [_row(r) for r in rows]

    # -- backtests ----------------------------------------------------------

    def save_backtest(self, profile: str, stats: dict, trades: list[dict],
                      start: str | None = None, end: str | None = None,
                      timeframe: str | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO backtests (created_at, profile, start_date, end_date,
                                          timeframe, stats, trades)
                   VALUES (?,?,?,?,?,?,?)""",
                (_now(), profile, start, end, timeframe, _j(stats), _j(trades)),
            )
            return int(cur.lastrowid)

    def backtests(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, profile, start_date, end_date, timeframe, stats "
                "FROM backtests ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row(r) for r in rows]

    def backtest(self, backtest_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM backtests WHERE id = ?", (backtest_id,)).fetchone()
        return _row(row) if row else None

    # -- alerts -------------------------------------------------------------

    def add_alert(self, kind: str, message: str, payload: dict | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO alerts (created_at, kind, message, payload) VALUES (?,?,?,?)",
                (_now(), kind, message, _j(payload)),
            )
            return int(cur.lastrowid)

    def alerts(self, limit: int = 50, unseen_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM alerts"
        if unseen_only:
            sql += " WHERE seen = 0"
        sql += " ORDER BY created_at DESC LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [_row(r) for r in rows]

    def mark_alerts_seen(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE alerts SET seen = 1 WHERE seen = 0")

    # -- settings -----------------------------------------------------------

    def set_setting(self, key: str, value) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, json.dumps(safe(value)), _now()),
            )

    def get_setting(self, key: str, default=None):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def all_settings(self) -> dict:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                out[r["key"]] = r["value"]
        return out


# --- helpers ---------------------------------------------------------------

# Columns holding JSON documents, decoded on the way out so callers never see
# raw strings.
JSON_COLUMNS = {
    "timeframe_reads", "evidence", "penalties", "waiting_for", "targets",
    "snapshot", "stats", "trades", "payload", "suggestion",
}


def _local_stamp(value) -> str | None:
    """The trader-facing form of a UTC timestamp, or None."""
    from .. import timeutil

    return timeutil.stamp(value) if value else None


def _fired_value(suggestion: dict, column: str):
    """Pull one fired_signals column out of a suggestion payload.

    The mapping is explicit rather than a dict lookup because most columns are
    flattened out of nested structures -- the entry zone, the target ladder,
    the opportunity assessment -- and a signal log that stores JSON blobs
    cannot be filtered or summed by the tab that reads it.
    """
    zone = suggestion.get("entry_zone") or {}
    targets = suggestion.get("targets") or []
    chance = suggestion.get("opportunity") or {}
    trigger = suggestion.get("trigger") or {}
    invalidation = suggestion.get("invalidation") or {}

    def target(i: int):
        return targets[i].get("price") if len(targets) > i else None

    mapping = {
        "uid": suggestion.get("id"),
        "fired_at": suggestion.get("created_at"),
        "fired_local": suggestion.get("fired_at"),
        "fired_date": suggestion.get("fired_date"),
        "fired_clock": suggestion.get("fired_clock"),
        "clock_offset": suggestion.get("clock_offset"),
        "session": suggestion.get("fired_session"),
        "session_label": suggestion.get("fired_session_label"),
        "session_quality": suggestion.get("fired_session_quality"),
        "regime": suggestion.get("regime_at_entry"),
        "entry_low": zone.get("low"),
        "entry_high": zone.get("high"),
        "entry_preferred": zone.get("preferred"),
        "stop": suggestion.get("initial_stop") or invalidation.get("price"),
        "tp1": target(0),
        "tp2": target(1),
        "tp3": target(2),
        "first_target_pips": chance.get("first_target_pips"),
        "max_pips": chance.get("max_pips"),
        "opportunity": chance.get("classification"),
        "trigger_text": trigger.get("description") or trigger.get("text"),
        "reasons": _j(suggestion.get("reasons")),
        "confirmations": _j(suggestion.get("confirmations")),
        "last_status": suggestion.get("status"),
    }
    if column in mapping:
        return mapping[column]
    value = suggestion.get(column)
    return _j(value) if isinstance(value, (list, dict)) else value


def _row(row: sqlite3.Row | None) -> dict:
    if row is None:
        return {}
    out = dict(row)
    for col in JSON_COLUMNS & out.keys():
        if isinstance(out[col], str):
            try:
                out[col] = json.loads(out[col])
            except json.JSONDecodeError:
                pass
    return out


def _j(value) -> str | None:
    return None if value is None else json.dumps(safe(value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _avg(values: list) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 2) if clean else None
