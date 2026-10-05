"""Ontopo POS API data model (SyncData) <-> connector domain models.

Spec: "Ontopo POS API Integration Specification".

SyncData   {syncId, syncAt (ISO 8601), order?, reservation}
Reservation{reservationId, duration (min), expectedDate "YYYYMMDD", expectedTime "HHmm",
            patron, size, status, table: string[], seatingRequests: string[]}
Order      {posOrderId, orderNumber, openedAt (ISO 8601), netTotal, discountAmount,
            dinnersCount, stage, tables: number[], items: [{itemId, itemName, quantity, price}]}
ReservationStatus: approved | seated | canceled | noShow | done | standby
ReservationStage:  preOrder | ordered | starters | main | check | paid

The spec does not define the Patron object, so several common field names are accepted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings
from ..models import Guest, InboundReservation, ReservationState

STATUS_APPROVED = "approved"
STATUS_SEATED = "seated"
STATUS_CANCELED = "canceled"
STATUS_NO_SHOW = "noShow"
STATUS_DONE = "done"
STATUS_STANDBY = "standby"

STAGE_ORDERED = "ordered"
STAGE_CHECK = "check"
STAGE_PAID = "paid"

# A waiting-list party has no table, so it must not hold one in Syrve.
_RELEASE_TABLE = {STATUS_CANCELED.lower(), "cancelled", STATUS_NO_SHOW.lower(), STATUS_STANDBY}


class PayloadError(ValueError):
    pass


def _get(data: dict, *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] not in (None, ""):
            return data[name]
    return default


def iso(value: datetime, tz: ZoneInfo) -> str:
    """Local naive (or aware) datetime -> ISO 8601 with the venue offset."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(tz).isoformat(timespec="seconds")


def parse_sync_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise PayloadError("SyncData must be a JSON object")
    sync_id = payload.get("syncId")
    if not sync_id:
        raise PayloadError("syncId is missing")
    return str(sync_id)


def parse_start(reservation: dict) -> datetime | None:
    date_raw, time_raw = reservation.get("expectedDate"), reservation.get("expectedTime")
    if not date_raw or not time_raw:
        return None
    date_s, time_s = str(date_raw).replace("-", ""), str(time_raw).replace(":", "").zfill(4)
    try:
        return datetime.strptime(date_s + time_s[:4], "%Y%m%d%H%M")
    except ValueError as exc:
        raise PayloadError(f"invalid expectedDate/expectedTime: {date_raw!r} {time_raw!r}") from exc


def parse_patron(patron: Any) -> Guest:
    if not isinstance(patron, dict):
        return Guest()
    first = _get(patron, "firstName", "first_name", "fname", default="")
    last = _get(patron, "lastName", "last_name", "lname", default="")
    if not first and not last:
        first = _get(patron, "name", "fullName", "full_name", "displayName", default="")
    phone = _get(patron, "phone", "phoneNumber", "phone_number", "mobile", "cellphone", default="")
    return Guest(
        first_name=str(first).strip(),
        last_name=str(last).strip(),
        phone=normalize_phone(str(phone)),
        email=str(_get(patron, "email", default="")).strip(),
    )


def parse_sync_data(payload: Any) -> InboundReservation:
    sync_id = parse_sync_id(payload)
    reservation = payload.get("reservation") or {}
    if not isinstance(reservation, dict):
        raise PayloadError("reservation must be an object")

    raw_status = str(reservation.get("status") or STATUS_APPROVED)
    status_key = raw_status.lower()
    if status_key in _RELEASE_TABLE:
        state = ReservationState.CANCELLED
    elif status_key == STATUS_DONE:
        state = ReservationState.DONE
    else:  # approved, seated
        state = ReservationState.ACTIVE

    start = parse_start(reservation)
    if state is ReservationState.ACTIVE and start is None:
        raise PayloadError("expectedDate/expectedTime are missing")

    try:
        size = int(reservation.get("size") or 0)
        duration = int(reservation["duration"]) if reservation.get("duration") else None
    except (TypeError, ValueError) as exc:
        raise PayloadError(f"invalid size/duration: {exc}") from exc
    if state is ReservationState.ACTIVE and size <= 0:
        raise PayloadError("size must be positive")

    return InboundReservation(
        sync_id=sync_id,
        reservation_id=str(reservation.get("reservationId") or ""),
        state=state,
        raw_status=raw_status,
        start=start,
        party_size=size,
        duration_minutes=duration,
        guest=parse_patron(reservation.get("patron")),
        table_labels=[str(t) for t in reservation.get("table") or []],
        seating_requests=[str(s) for s in reservation.get("seatingRequests") or []],
        raw=reservation,
    )


def parse_sync_list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "reservations", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise PayloadError("unexpected reservationsForToday response format")


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


def build_sync_data(sync_id: str, sync_at: datetime, tz: ZoneInfo, reservation: dict, order: dict | None = None) -> dict:
    body = {"syncId": sync_id, "syncAt": iso(sync_at, tz), "reservation": reservation}
    if order is not None:
        body["order"] = order
    return body
