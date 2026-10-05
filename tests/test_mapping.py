from datetime import datetime, timezone

import pytest

from connector.models import ReservationState
from connector.ontopo.mapping import PayloadError, build_sync_data, normalize_phone, parse_sync_data

from .fakes import make_settings

S = make_settings()


def sync(status="approved", **reservation):
    base = {
        "reservationId": "ba788d3f",
        "duration": 120,
        "expectedDate": "20250806",
        "expectedTime": "1830",
        "patron": {"firstName": "Dana", "lastName": "Levi", "phone": "050-123-4567"},
        "size": 6,
        "status": status,
        "table": ["5", "6"],
        "seatingRequests": ["bar"],
    }
    base.update(reservation)
    return {"syncId": "S1", "syncAt": "2025-08-06T10:00:00Z", "reservation": base}


def test_parse_spec_example():
    res = parse_sync_data(sync())
    assert res.sync_id == "S1" and res.reservation_id == "ba788d3f"
    assert res.start == datetime(2025, 8, 6, 18, 30)
    assert res.party_size == 6 and res.duration_minutes == 120
    assert res.table_labels == ["5", "6"] and res.seating_requests == ["bar"]
    assert res.guest.full_name == "Dana Levi" and res.guest.phone == "+972501234567"
    assert res.state is ReservationState.ACTIVE


@pytest.mark.parametrize(
    "status,state",
    [
        ("approved", ReservationState.ACTIVE),
        ("seated", ReservationState.ACTIVE),
        ("canceled", ReservationState.CANCELLED),
        ("noShow", ReservationState.CANCELLED),
        ("standby", ReservationState.CANCELLED),
        ("done", ReservationState.DONE),
    ],
)
def test_status_mapping(status, state):
    assert parse_sync_data(sync(status)).state is state


def test_patron_with_single_name_field():
    res = parse_sync_data(sync(patron={"name": "Avi", "phone": "+44 7700 900123"}))
    assert res.guest.first_name == "Avi" and res.guest.phone == "+447700900123"


def test_invalid_payloads():
    with pytest.raises(PayloadError):
        parse_sync_data({"reservation": {}})  # no syncId
    with pytest.raises(PayloadError):
        parse_sync_data(sync(expectedDate=None))
    with pytest.raises(PayloadError):
        parse_sync_data(sync(expectedTime="25:99"))
    # a cancellation does not need date or size
    assert parse_sync_data(sync("canceled", expectedDate=None, size=0)).state is ReservationState.CANCELLED


def test_build_sync_data_uses_venue_offset():
    body = build_sync_data("S1", datetime(2030, 1, 7, 17, 0, tzinfo=timezone.utc), S.tz, {"status": "seated"})
    assert body == {"syncId": "S1", "syncAt": "2030-01-07T19:00:00+02:00", "reservation": {"status": "seated"}}


@pytest.mark.parametrize(
    "raw,expected",
    [("+972 50-123-4567", "+972501234567"), ("0501234567", "+972501234567"), ("00447700900123", "+447700900123"), ("", "")],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected
