"""Syrve table directory, Ontopo table-label mapping and free-table selection."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterable

from .config import Settings
from .models import BusyInterval, Table

log = logging.getLogger(__name__)


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
    free = [t for t in tables if is_table_free(t.id, start, end, busy, buffer, ignore_source_ids)]
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


class TableDirectory:
    """Cached Syrve tables plus translation to and from Ontopo table labels."""

    CACHE_SECONDS = 600

    def __init__(self, settings: Settings, syrve):
        self.settings = settings
        self.syrve = syrve
        self._tables: list[Table] = []
        self._loaded_at: datetime | None = None

    async def tables(self, refresh: bool = False) -> list[Table]:
        stale = self._loaded_at is None or datetime.now() - self._loaded_at > timedelta(seconds=self.CACHE_SECONDS)
        if refresh or stale:
            self._tables = await self.syrve.tables()
            self._loaded_at = datetime.now()
        return self._tables

    def by_id(self) -> dict[str, Table]:
        return {t.id: t for t in self._tables}

    def resolve_label(self, label: str) -> Table | None:
        """Ontopo table label ("5") -> Syrve table, via table_map, then number, then name."""
        target = self.settings.reservations.table_map.get(label, label)
        for t in self._tables:
            if t.id == target:
                return t
        for t in self._tables:
            if t.number is not None and str(t.number) == target:
                return t
        for t in self._tables:
            if t.name == target:
                return t
        return None

    def label_for(self, table: Table) -> str:
        """Syrve table -> Ontopo table label (reverse table_map, else the table number)."""
        for label, target in self.settings.reservations.table_map.items():
            if target in (table.id, str(table.number), table.name):
                return label
        return str(table.number) if table.number is not None else table.name
