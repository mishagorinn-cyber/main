"""Free-table calculation and the Syrve -> Ontopo availability sync."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from .config import AvailabilityConfig, Settings
from .models import AvailabilitySlot, BusyInterval, Table

log = logging.getLogger(__name__)


def opening_window(day: date, config: AvailabilityConfig) -> tuple[datetime, datetime] | None:
    hours = config.hours_for(day.weekday())
    if hours is None:
        return None
    opens = datetime.combine(day, hours.open)
    closes = datetime.combine(day, hours.close)
    if closes <= opens:  # closes after midnight
        closes += timedelta(days=1)
    return opens, closes


def is_table_free(
    table_id: str,
    start: datetime,
    end: datetime,
    busy: Iterable[BusyInterval],
    buffer: timedelta = timedelta(0),
    ignore_source_ids: frozenset[str] = frozenset(),
) -> bool:
    for interval in busy:
        if table_id not in interval.table_ids or interval.source_id in ignore_source_ids:
            continue
        if start < interval.end + buffer and interval.start < end + buffer:
            return False
    return True


def compute_slots(
    tables: list[Table],
    busy: list[BusyInterval],
    config: AvailabilityConfig,
    now: datetime,
    first_day: date | None = None,
    days: int | None = None,
) -> list[AvailabilitySlot]:
    """For each bookable start time, which tables can seat a guest for the default duration."""
    first_day = first_day or now.date()
    days = days or config.horizon_days
    step = timedelta(minutes=config.slot_minutes)
    duration = timedelta(minutes=config.default_duration_minutes)
    buffer = timedelta(minutes=config.buffer_minutes)
    last_offset = timedelta(minutes=config.last_seating_offset_minutes)

    by_table: dict[str, list[BusyInterval]] = {t.id: [] for t in tables}
    for interval in busy:
        for table_id in interval.table_ids:
            if table_id in by_table:
                by_table[table_id].append(interval)

    slots: list[AvailabilitySlot] = []
    for offset in range(days):
        window = opening_window(first_day + timedelta(days=offset), config)
        if window is None:
            continue
        opens, closes = window
        slot = opens
        while slot <= closes - last_offset:
            if slot >= now:
                free = tuple(
                    t.id for t in tables if is_table_free(t.id, slot, slot + duration, by_table[t.id], buffer)
                )
                slots.append(AvailabilitySlot(slot, free))
            slot += step
    return slots


def pick_tables(
    tables: list[Table],
    busy: list[BusyInterval],
    start: datetime,
    duration_minutes: int,
    party_size: int,
    buffer_minutes: int = 0,
    allow_combining: bool = False,
    ignore_source_ids: frozenset[str] = frozenset(),
) -> list[Table]:
    """Best-fit table choice: the smallest free table that seats the party.

    With combining enabled and no single table large enough, the largest free
    tables of one section are joined until they seat the party.
    """
    end = start + timedelta(minutes=duration_minutes)
    buffer = timedelta(minutes=buffer_minutes)
    free = [
        t for t in tables if is_table_free(t.id, start, end, busy, buffer, ignore_source_ids)
    ]
    single = sorted((t for t in free if t.capacity >= party_size), key=lambda t: (t.capacity, t.number or 0))
    if single:
        return [single[0]]
    if not allow_combining:
        return []
    best: list[Table] = []
    sections: dict[str, list[Table]] = {}
    for t in free:
        sections.setdefault(t.section_id, []).append(t)
    for section_tables in sections.values():
        chosen: list[Table] = []
        seats = 0
        for t in sorted(section_tables, key=lambda t: -t.capacity):
            if seats >= party_size:
                break
            chosen.append(t)
            seats += t.capacity
        if seats >= party_size and (not best or len(chosen) < len(best)):
            best = chosen
    return best


class AvailabilitySync:
    """Reads tables and reserves from Syrve, pushes free slots to Ontopo when they change."""

    def __init__(self, settings: Settings, syrve, ontopo, store):
        self.settings = settings
        self.syrve = syrve
        self.ontopo = ontopo
        self.store = store
        self._tables: list[Table] | None = None

    async def load_tables(self, refresh: bool = False) -> list[Table]:
        if self._tables is None or refresh:
            self._tables = await self.syrve.tables()
        return self._tables

    async def load_busy(self, tables: list[Table], date_from: datetime, date_to: datetime) -> list[BusyInterval]:
        section_ids = sorted({t.section_id for t in tables})
        if not section_ids:
            return []
        busy: list[BusyInterval] = []
        chunk = timedelta(days=7)
        cursor = date_from
        while cursor < date_to:
            chunk_end = min(cursor + chunk, date_to)
            busy.extend(await self.syrve.workload(section_ids, cursor, chunk_end))
            cursor = chunk_end
        if self.settings.syrve.consider_open_orders:
            busy.extend(await self._open_order_intervals(tables))
        return busy

    async def _open_order_intervals(self, tables: list[Table]) -> list[BusyInterval]:
        now = self.now()
        try:
            orders = await self.syrve.open_orders_on_tables([t.id for t in tables], now - timedelta(days=1))
        except Exception:  # degrade gracefully: reserves alone still give useful availability
            log.exception("Could not read open orders from Syrve")
            return []
        duration = timedelta(minutes=self.settings.availability.default_duration_minutes)
        intervals = []
        for order in orders:
            table_ids = order.get("tableIds") or []
            if not table_ids:
                continue
            created = order.get("whenCreated")
            started = None
            if created:
                try:
                    started = datetime.fromisoformat(created.replace("T", " ")).replace(tzinfo=None)
                except ValueError:
                    started = None
            end = max((started or now) + duration, now + timedelta(minutes=30))
            intervals.append(BusyInterval(tuple(table_ids), now, end, source_id=order.get("id", "")))
        return intervals

    def now(self) -> datetime:
        return datetime.now(self.settings.tz).replace(tzinfo=None, second=0, microsecond=0)

    async def build_payload(self, refresh_tables: bool = False) -> dict:
        from .ontopo.mapping import build_availability_payload

        tables = await self.load_tables(refresh=refresh_tables)
        now = self.now()
        cfg = self.settings.availability
        date_from = datetime.combine(now.date(), datetime.min.time())
        date_to = date_from + timedelta(days=cfg.horizon_days + 1)
        busy = await self.load_busy(tables, date_from, date_to)
        slots = compute_slots(tables, busy, cfg, now)
        return build_availability_payload(self.settings, tables, slots, now)

    async def run(self, force: bool = False, refresh_tables: bool = True) -> bool:
        """One sync pass. Returns True if Ontopo was updated."""
        payload = await self.build_payload(refresh_tables=refresh_tables)
        digest = payload_digest(payload)
        if not force and digest == self.store.get_meta("availability_digest"):
            self.store.set_meta("availability_checked_at", datetime.now(timezone.utc).isoformat())
            return False
        await self.ontopo.push_availability(payload)
        self.store.set_meta("availability_digest", digest)
        self.store.set_meta("availability_pushed_at", datetime.now(timezone.utc).isoformat())
        self.store.set_meta("availability_checked_at", datetime.now(timezone.utc).isoformat())
        log.info("Pushed availability to Ontopo: %d slots", len(payload.get("slots", [])))
        return True


def payload_digest(payload: dict) -> str:
    stable = {k: v for k, v in payload.items() if k != "generatedAt"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()
