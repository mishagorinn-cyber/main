"""Platform-neutral domain models shared by the Syrve and Ontopo sides.

All datetimes in this module are *naive local time* of the restaurant
(Settings.timezone): that is how Syrve stores reserves, and it keeps the
availability arithmetic free of DST surprises.
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
    """A period during which tables are taken (a Syrve reserve or an open order)."""

    table_ids: tuple[str, ...]
    start: datetime
    end: datetime
    source_id: str = ""  # Syrve reserve/order id

    @classmethod
    def from_duration(cls, table_ids, start: datetime, minutes: int, source_id: str = "") -> "BusyInterval":
        return cls(tuple(table_ids), start, start + timedelta(minutes=minutes), source_id)


@dataclass(frozen=True)
class AvailabilitySlot:
    start: datetime
    free_table_ids: tuple[str, ...]


class ReservationState(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"


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
    """A reservation as received from Ontopo, normalised."""

    id: str
    state: ReservationState
    start: datetime  # naive local time
    party_size: int
    duration_minutes: int | None = None
    guest: Guest = field(default_factory=Guest)
    notes: str = ""
    table_ids: list[str] = field(default_factory=list)  # Ontopo-side table ids
    raw_status: str = ""
    updated_at: datetime | None = None

    def fingerprint(self) -> str:
        """Fields that, when changed, require the Syrve reserve to be re-created."""
        parts = [
            self.start.isoformat(),
            str(self.party_size),
            str(self.duration_minutes or ""),
            ",".join(sorted(self.table_ids)),
            self.guest.full_name,
            self.guest.phone,
            self.notes,
        ]
        return "|".join(parts)
