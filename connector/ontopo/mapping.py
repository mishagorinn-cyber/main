"""Translation between Ontopo payloads and the connector's domain models.

Ontopo does not publish its partner (POS) API, so the wire format below is
the connector's documented contract (see README, "Ontopo contract"). All
field names Ontopo might use live in this one module: when Ontopo hands over
its integration spec, adjust the functions here and nothing else.

Parsing accepts both camelCase and snake_case keys.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings
from ..models import AvailabilitySlot, Guest, InboundReservation, ReservationState, Table

CANCELLED_STATUSES = {"cancelled", "canceled", "rejected", "deleted", "no_show", "noshow", "declined", "expired"}


class PayloadError(ValueError):
    pass


def _get(data: dict, *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return default


def _to_local(value: str, tz: ZoneInfo) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PayloadError(f"invalid datetime: {value!r}") from exc
    if dt.tzinfo is None:  # already local
        return dt.replace(second=0, microsecond=0)
    return dt.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0)


def _iso_local(value: datetime, tz: ZoneInfo) -> str:
    return value.replace(tzinfo=tz).isoformat()


# --------------------------------------------------------------------- inbound


def unwrap_event(payload: dict) -> tuple[str, dict]:
    """Return (event type, reservation dict) from a webhook body."""
    if not isinstance(payload, dict):
        raise PayloadError("payload must be a JSON object")
    event = str(_get(payload, "event", "type", "eventType", default="reservation.updated"))
    reservation = _get(payload, "reservation", "data", "booking")
    if reservation is None and _get(payload, "id", "reservationId", "reservation_id"):
        reservation = payload  # bare reservation object
    if not isinstance(reservation, dict):
        raise PayloadError("no reservation object in payload")
    return event, reservation


def parse_reservation(data: dict, settings: Settings, event: str = "") -> InboundReservation:
    tz = settings.tz
    res_id = _get(data, "id", "reservationId", "reservation_id")
    if not res_id:
        raise PayloadError("reservation id is missing")

    raw_status = str(_get(data, "status", "state", default="confirmed")).lower()
    cancelled = raw_status in CANCELLED_STATUSES or event.endswith((".cancelled", ".canceled", ".deleted"))

    start_raw = _get(data, "start", "startTime", "start_time", "dateTime", "datetime", "time")
    if not start_raw:
        date_part, time_part = _get(data, "date"), _get(data, "hour", "timeOfDay")
        if date_part and time_part:
            start_raw = f"{date_part}T{time_part}"
    if not start_raw:
        raise PayloadError("reservation start time is missing")
    start = _to_local(str(start_raw), tz)

    duration = _get(data, "durationMinutes", "duration_minutes", "duration")
    end_raw = _get(data, "end", "endTime", "end_time")
    if duration is None and end_raw:
        duration = int((_to_local(str(end_raw), tz) - start) / timedelta(minutes=1))

    party = _get(data, "partySize", "party_size", "guests", "size", "covers", default=0)
    if isinstance(party, dict):
        party = _get(party, "count", "total", default=0)
    try:
        party = int(party)
    except (TypeError, ValueError) as exc:
        raise PayloadError(f"invalid party size: {party!r}") from exc
    if party <= 0 and not cancelled:
        raise PayloadError("party size must be positive")

    g = _get(data, "guest", "customer", "contact", default={}) or {}
    first = _get(g, "firstName", "first_name", default="")
    last = _get(g, "lastName", "last_name", default="")
    if not first and not last:
        first = _get(g, "name", "fullName", "full_name", default="")
    guest = Guest(
        first_name=str(first).strip(),
        last_name=str(last).strip(),
        phone=normalize_phone(str(_get(g, "phone", "phoneNumber", "phone_number", "mobile", default=""))),
        email=str(_get(g, "email", default="")).strip(),
    )

    tables = _get(data, "tableIds", "table_ids", "tables", default=[]) or []
    table_ids = [str(t.get("id") if isinstance(t, dict) else t) for t in tables]

    updated_raw = _get(data, "updatedAt", "updated_at", "modifiedAt")
    updated = _to_local(str(updated_raw), tz) if updated_raw else None

    return InboundReservation(
        id=str(res_id),
        state=ReservationState.CANCELLED if cancelled else ReservationState.ACTIVE,
        start=start,
        party_size=party,
        duration_minutes=int(duration) if duration else None,
        guest=guest,
        notes=str(_get(data, "notes", "comment", "comments", "specialRequests", default="")).strip(),
        table_ids=table_ids,
        raw_status=raw_status,
        updated_at=updated,
    )


def parse_reservation_list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        items = _get(payload, "reservations", "items", "data", default=[])
        if isinstance(items, list):
            return items
    raise PayloadError("unexpected reservations list format")


def normalize_phone(phone: str) -> str:
    """Syrve requires an international number starting with '+'."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not digits:
        return ""
    if phone.strip().startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):  # Israeli local format 05X-XXXXXXX
        return "+972" + digits[1:]
    return "+" + digits


# -------------------------------------------------------------------- outbound


def build_availability_payload(
    settings: Settings, tables: list[Table], slots: list[AvailabilitySlot], generated_at: datetime
) -> dict:
    tz = settings.tz
    capacity = {t.id: t.capacity for t in tables}
    return {
        "venueId": settings.ontopo.venue_id,
        "source": "syrve",
        "timezone": settings.timezone,
        "generatedAt": _iso_local(generated_at, tz),
        "slotMinutes": settings.availability.slot_minutes,
        "defaultDurationMinutes": settings.availability.default_duration_minutes,
        "tables": [
            {
                "id": t.id,
                "number": t.number,
                "name": t.name,
                "capacity": t.capacity,
                "areaId": t.section_id,
                "areaName": t.section_name,
            }
            for t in tables
        ],
        "slots": [
            {
                "start": _iso_local(s.start, tz),
                "freeTableIds": list(s.free_table_ids),
                "freeSeats": sum(capacity.get(t, 0) for t in s.free_table_ids),
                "maxPartySize": max((capacity.get(t, 0) for t in s.free_table_ids), default=0),
            }
            for s in slots
        ],
    }


def build_status_payload(
    reservation_id: str, status: str, syrve_reserve_id: str = "", tables: list[Table] | None = None, message: str = ""
) -> dict:
    """Report back to Ontopo what happened on the POS ("confirmed" / "rejected" / "cancelled")."""
    return {
        "reservationId": reservation_id,
        "status": status,
        "posReservationId": syrve_reserve_id or None,
        "tables": [{"id": t.id, "name": t.name, "number": t.number} for t in (tables or [])],
        "message": message or None,
    }
