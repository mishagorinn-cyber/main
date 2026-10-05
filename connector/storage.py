"""SQLite persistence: webhook inbox, syncId <-> Syrve links, key/value meta."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_id         TEXT NOT NULL,
    source          TEXT NOT NULL,                   -- webhook | poll
    payload         TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS events_due ON events (status, next_attempt_at);
CREATE TABLE IF NOT EXISTS links (
    sync_id               TEXT PRIMARY KEY,
    origin                TEXT NOT NULL,   -- ontopo (reservation made in Ontopo) | pos (walk-in order)
    state                 TEXT NOT NULL,   -- active | cancelled | done | failed
    ontopo_reservation_id TEXT,
    ontopo_reservation    TEXT,            -- last reservation object seen, JSON
    syrve_reserve_id      TEXT,
    syrve_order_id        TEXT,
    fingerprint           TEXT,
    start                 TEXT,
    duration_minutes      INTEGER,
    party_size            INTEGER,
    table_ids             TEXT,            -- Syrve table ids, JSON
    order_digest          TEXT,            -- last order state sent to Ontopo
    error                 TEXT,
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS links_order ON links (syrve_order_id);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    id: int
    sync_id: str
    source: str
    payload: dict
    attempts: int


@dataclass
class Link:
    sync_id: str
    origin: str
    state: str
    ontopo_reservation_id: str | None = None
    ontopo_reservation: dict = field(default_factory=dict)
    syrve_reserve_id: str | None = None
    syrve_order_id: str | None = None
    fingerprint: str | None = None
    start: datetime | None = None
    duration_minutes: int | None = None
    party_size: int | None = None
    table_ids: list[str] = field(default_factory=list)
    order_digest: str | None = None
    error: str | None = None
    updated_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat() if self.start else None
        return d


_LINK_FIELDS = [f.name for f in fields(Link)]


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

    def enqueue(self, sync_id: str, source: str, payload: dict) -> int:
        now = _utcnow()
        cur = self._exec(
            "INSERT INTO events (sync_id, source, payload, received_at, next_attempt_at) VALUES (?, ?, ?, ?, ?)",
            (sync_id, source, json.dumps(payload), now, now),
        )
        return int(cur.lastrowid)

    def due_events(self, limit: int = 50) -> list[Event]:
        """Pending events whose retry time has come, oldest first.

        Only the oldest pending event of each syncId is returned so that the
        updates of one reservation are always applied in order.
        """
        rows = self._exec(
            """
            SELECT e.* FROM events e
            WHERE e.status = 'pending' AND e.next_attempt_at <= ?
              AND e.id = (SELECT MIN(id) FROM events p WHERE p.sync_id = e.sync_id AND p.status = 'pending')
            ORDER BY e.id LIMIT ?
            """,
            (_utcnow(), limit),
        ).fetchall()
        return [Event(r["id"], r["sync_id"], r["source"], json.loads(r["payload"]), r["attempts"]) for r in rows]

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
            "SELECT id, sync_id, source, attempts, last_error, received_at FROM events"
            " WHERE status = 'failed' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def purge_done_events(self, older_than_days: int = 14) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        return self._exec("DELETE FROM events WHERE status = 'done' AND received_at < ?", (cutoff,)).rowcount

    # ---------------------------------------------------------------- links

    def get_link(self, sync_id: str) -> Link | None:
        row = self._exec("SELECT * FROM links WHERE sync_id = ?", (sync_id,)).fetchone()
        return self._link(row) if row else None

    def link_for_order(self, order_id: str) -> Link | None:
        row = self._exec("SELECT * FROM links WHERE syrve_order_id = ?", (order_id,)).fetchone()
        return self._link(row) if row else None

    def links_starting_between(self, start: datetime, end: datetime) -> list[Link]:
        rows = self._exec(
            "SELECT * FROM links WHERE origin = 'ontopo' AND start >= ? AND start <= ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [self._link(r) for r in rows]

    def open_order_links(self) -> list[Link]:
        rows = self._exec(
            "SELECT * FROM links WHERE syrve_order_id IS NOT NULL AND state = 'active'"
        ).fetchall()
        return [self._link(r) for r in rows]

    def update_link(self, sync_id: str, **changes) -> Link:
        """Insert or merge fields into the link for sync_id."""
        link = self.get_link(sync_id) or Link(sync_id=sync_id, origin=changes.get("origin", "ontopo"), state="active")
        for key, value in changes.items():
            if key not in _LINK_FIELDS:
                raise KeyError(key)
            setattr(link, key, value)
        link.updated_at = _utcnow()
        values = [
            link.sync_id,
            link.origin,
            link.state,
            link.ontopo_reservation_id,
            json.dumps(link.ontopo_reservation or {}),
            link.syrve_reserve_id,
            link.syrve_order_id,
            link.fingerprint,
            link.start.isoformat() if link.start else None,
            link.duration_minutes,
            link.party_size,
            json.dumps(link.table_ids or []),
            link.order_digest,
            link.error,
            link.updated_at,
        ]
        placeholders = ", ".join("?" for _ in _LINK_FIELDS)
        updates = ", ".join(f"{name} = excluded.{name}" for name in _LINK_FIELDS[1:])
        self._exec(
            f"INSERT INTO links ({', '.join(_LINK_FIELDS)}) VALUES ({placeholders})"
            f" ON CONFLICT(sync_id) DO UPDATE SET {updates}",
            tuple(values),
        )
        return link

    def recent_links(self, limit: int = 50) -> list[Link]:
        rows = self._exec("SELECT * FROM links ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._link(r) for r in rows]

    @staticmethod
    def _link(row: sqlite3.Row) -> Link:
        return Link(
            sync_id=row["sync_id"],
            origin=row["origin"],
            state=row["state"],
            ontopo_reservation_id=row["ontopo_reservation_id"],
            ontopo_reservation=json.loads(row["ontopo_reservation"] or "{}"),
            syrve_reserve_id=row["syrve_reserve_id"],
            syrve_order_id=row["syrve_order_id"],
            fingerprint=row["fingerprint"],
            start=datetime.fromisoformat(row["start"]) if row["start"] else None,
            duration_minutes=row["duration_minutes"],
            party_size=row["party_size"],
            table_ids=json.loads(row["table_ids"] or "[]"),
            order_digest=row["order_digest"],
            error=row["error"],
            updated_at=row["updated_at"],
        )
