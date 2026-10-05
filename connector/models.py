"""Platform-neutral domain models shared by the Syrve and Ontopo sides.

All datetimes in this module are *naive local time* of the restaurant
(Settings.timezone): Syrve stores reserves that way and Ontopo sends
reservation date/time without an offset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


@dataclass(frozen=True)
class Table:
    id: str
    number: int | None
    name: str
    capacity: int
    section_id: str
    section_name: str = ""


@dataclass(frozen=True)
class BusyInterval:
    """A period during which tables are taken by a Syrve reserve."""

    table_ids: tuple[str, ...]
    start: datetime
    end: datetime
    source_id: str = ""  # Syrve reserve id

    @classmethod
    def from_duration(cls, table_ids, start: datetime, minutes: int, source_id: str = "") -> "BusyInterval":
        return cls(tuple(table_ids), start, start + timedelta(minutes=minutes), source_id)


class ReservationState(str, Enum):
    ACTIVE = "active"        # needs a table in Syrve
    CANCELLED = "cancelled"  # the Syrve reserve must be cancelled
    DONE = "done"            # finished: leave Syrve as it is


@dataclass
class Guest:
    first_name: str = ""
    last_name: str = ""
    phone: str = ""
    email: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()


@dataclass
class InboundReservation:
    """An Ontopo SyncData record, normalised."""

    sync_id: str
    reservation_id: str  # Ontopo's own id
    state: ReservationState
    raw_status: str
    start: datetime | None  # naive local time
    party_size: int
    duration_minutes: int | None = None
    guest: Guest = field(default_factory=Guest)
    table_labels: list[str] = field(default_factory=list)  # Ontopo table identifiers
    seating_requests: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)  # the reservation object as received

    def fingerprint(self, table_labels: list[str] | None = None) -> str:
        """Fields that, when changed, require the Syrve reserve to be re-created."""
        labels = self.table_labels if table_labels is None else table_labels
        parts = [
            self.start.isoformat() if self.start else "",
            str(self.party_size),
            str(self.duration_minutes or ""),
            ",".join(sorted(labels)),
            self.guest.full_name,
            self.guest.phone,
            ",".join(self.seating_requests),
        ]
        return "|".join(parts)
