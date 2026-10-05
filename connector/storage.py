"""SQLite persistence: webhook inbox, Ontopo<->Syrve reservation mapping, key/value meta."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id  TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    payload         TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS events_due ON events (status, next_attempt_at);
CREATE TABLE IF NOT EXISTS reservations (
    ontopo_id        TEXT PRIMARY KEY,
    syrve_reserve_id TEXT,
    state            TEXT NOT NULL,          -- active | cancelled | failed
    fingerprint      TEXT,
    start            TEXT,
    party_size       INTEGER,
    table_ids        TEXT,
    error            TEXT,
    updated_at       TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    id: int
    reservation_id: str
    event_type: str
    payload: dict
    attempts: int


@dataclass
class ReservationLink:
    ontopo_id: str
    syrve_reserve_id: str | None
    state: str
    fingerprint: str | None
    start: str | None
    party_size: int | None
    table_ids: list[str]
    error: str | None
    updated_at: str


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    # ---------------------------------------------------------------- meta

    def get_meta(self, key: str) -> str | None:
        row = self._exec("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---------------------------------------------------------------- inbox

    def enqueue(self, reservation_id: str, event_type: str, payload: dict) -> int:
        now = _utcnow()
        cur = self._exec(
            "INSERT INTO events (reservation_id, event_type, payload, received_at, next_attempt_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (reservation_id, event_type, json.dumps(payload), now, now),
        )
        return int(cur.lastrowid)

    def due_events(self, limit: int = 50) -> list[Event]:
        """Pending events whose retry time has come, oldest first.

        Only the oldest pending event of each reservation is returned so that
        events for one reservation are always applied in order.
        """
        rows = self._exec(
            """
            SELECT e.* FROM events e
            WHERE e.status = 'pending' AND e.next_attempt_at <= ?
              AND e.id = (SELECT MIN(id) FROM events p
                          WHERE p.reservation_id = e.reservation_id AND p.status = 'pending')
            ORDER BY e.id LIMIT ?
            """,
            (_utcnow(), limit),
        ).fetchall()
        return [
            Event(r["id"], r["reservation_id"], r["event_type"], json.loads(r["payload"]), r["attempts"])
            for r in rows
        ]

    def mark_done(self, event_id: int) -> None:
        self._exec("UPDATE events SET status = 'done', last_error = NULL WHERE id = ?", (event_id,))

    def mark_retry(self, event_id: int, error: str, delay_seconds: float, give_up: bool) -> None:
        when = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
        self._exec(
            "UPDATE events SET attempts = attempts + 1, last_error = ?, next_attempt_at = ?, status = ? WHERE id = ?",
            (error[:2000], when, "failed" if give_up else "pending", event_id),
        )

    def retry_failed(self) -> int:
        cur = self._exec(
            "UPDATE events SET status = 'pending', attempts = 0, next_attempt_at = ? WHERE status = 'failed'",
            (_utcnow(),),
        )
        return cur.rowcount

    def event_counts(self) -> dict[str, int]:
        rows = self._exec("SELECT status, COUNT(*) AS n FROM events GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def failed_events(self, limit: int = 50) -> list[dict]:
        rows = self._exec(
            "SELECT id, reservation_id, event_type, attempts, last_error, received_at FROM events"
            " WHERE status = 'failed' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- mapping

    def get_link(self, ontopo_id: str) -> ReservationLink | None:
        row = self._exec("SELECT * FROM reservations WHERE ontopo_id = ?", (ontopo_id,)).fetchone()
        return self._link(row) if row else None

    def save_link(
        self,
        ontopo_id: str,
        *,
        state: str,
        syrve_reserve_id: str | None = None,
        fingerprint: str | None = None,
        start: datetime | None = None,
        party_size: int | None = None,
        table_ids: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        self._exec(
            """
            INSERT INTO reservations
                (ontopo_id, syrve_reserve_id, state, fingerprint, start, party_size, table_ids, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ontopo_id) DO UPDATE SET
                syrve_reserve_id = excluded.syrve_reserve_id, state = excluded.state,
                fingerprint = excluded.fingerprint, start = excluded.start,
                party_size = excluded.party_size, table_ids = excluded.table_ids,
                error = excluded.error, updated_at = excluded.updated_at
            """,
            (
                ontopo_id,
                syrve_reserve_id,
                state,
                fingerprint,
                start.isoformat() if start else None,
                party_size,
                json.dumps(table_ids or []),
                error,
                _utcnow(),
            ),
        )

    def recent_links(self, limit: int = 50) -> list[ReservationLink]:
        rows = self._exec("SELECT * FROM reservations ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._link(r) for r in rows]

    @staticmethod
    def _link(row: sqlite3.Row) -> ReservationLink:
        return ReservationLink(
            ontopo_id=row["ontopo_id"],
            syrve_reserve_id=row["syrve_reserve_id"],
            state=row["state"],
            fingerprint=row["fingerprint"],
            start=row["start"],
            party_size=row["party_size"],
            table_ids=json.loads(row["table_ids"] or "[]"),
            error=row["error"],
            updated_at=row["updated_at"],
        )
