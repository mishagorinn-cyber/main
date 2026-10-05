from datetime import datetime

import pytest

from connector.models import ReservationState
from connector.ontopo.mapping import PayloadError, normalize_phone, parse_reservation, unwrap_event

from .fakes import make_settings

S = make_settings()


def test_parse_camel_case_with_timezone_conversion():
    event, data = unwrap_event(
        {
            "event": "reservation.created",
            "reservation": {
                "id": "R1",
                "status": "confirmed",
                "startTime": "2030-01-07T17:00:00Z",
                "partySize": 4,
                "guest": {"firstName": "Dana", "lastName": "Levi", "phone": "050-123-4567"},
                "notes": "window seat",
                "tableIds": ["A1"],
            },
        }
    )
    res = parse_reservation(data, S, event)
    assert res.start == datetime(2030, 1, 7, 19, 0)  # UTC+2 in January
    assert res.party_size == 4
    assert res.guest.phone == "+972501234567"
    assert res.guest.full_name == "Dana Levi"
    assert res.table_ids == ["A1"]
    assert res.state is ReservationState.ACTIVE


def test_parse_snake_case_end_time_and_cancel_event():
    event, data = unwrap_event(
        {
            "type": "reservation.cancelled",
            "data": {
                "reservation_id": "R2",
                "start_time": "2030-01-07T19:00:00+02:00",
                "end_time": "2030-01-07T20:30:00+02:00",
                "guests": {"count": 2},
                "customer": {"name": "Avi"},
            },
        }
    )
    res = parse_reservation(data, S, event)
    assert res.duration_minutes == 90
    assert res.party_size == 2
    assert res.guest.first_name == "Avi"
    assert res.state is ReservationState.CANCELLED


def test_missing_fields_raise():
    with pytest.raises(PayloadError):
        parse_reservation({"id": "R3", "partySize": 2}, S)
    with pytest.raises(PayloadError):
        unwrap_event({"event": "x"})


@pytest.mark.parametrize(
    "raw,expected",
    [("+972 50-123-4567", "+972501234567"), ("0501234567", "+972501234567"), ("00447700900123", "+447700900123"), ("", "")],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected
