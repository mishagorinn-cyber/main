from datetime import date, datetime, time

from connector.availability import compute_slots, opening_window, pick_tables
from connector.config import AvailabilityConfig, DayHours
from connector.models import BusyInterval, Table

TABLES = [
    Table("t1", 1, "1", 2, "s1"),
    Table("t2", 2, "2", 4, "s1"),
    Table("t3", 3, "3", 6, "s1"),
]
DAY = date(2030, 1, 7)  # Monday


def cfg(**kw):
    base = dict(
        slot_minutes=30,
        default_duration_minutes=120,
        buffer_minutes=0,
        last_seating_offset_minutes=120,
        opening_hours={"default": DayHours(open=time(18), close=time(23))},
    )
    base.update(kw)
    return AvailabilityConfig(**base)


def at(h, m=0, day=DAY):
    return datetime.combine(day, time(h, m))


def test_slots_exclude_busy_tables():
    busy = [BusyInterval.from_duration(["t2"], at(19), 120)]
    slots = compute_slots(TABLES, busy, cfg(), now=at(0), first_day=DAY, days=1)
    by_start = {s.start: s.free_table_ids for s in slots}
    assert [s.start for s in slots] == [at(18), at(18, 30), at(19), at(19, 30), at(20), at(20, 30), at(21)]
    assert by_start[at(18)] == ("t1", "t3")       # 18:00-20:00 overlaps 19:00-21:00
    assert by_start[at(21)] == ("t1", "t2", "t3")  # t2 is free again at 21:00


def test_buffer_extends_busy_interval():
    busy = [BusyInterval.from_duration(["t1"], at(18), 120)]
    slots = compute_slots(TABLES, busy, cfg(buffer_minutes=30), now=at(0), first_day=DAY, days=1)
    by_start = {s.start: s.free_table_ids for s in slots}
    assert "t1" not in by_start[at(20)]
    assert "t1" in by_start[at(20, 30)]


def test_past_slots_and_closed_days_are_skipped():
    hours = {"default": DayHours(open=time(18), close=time(23)), "tue": None}
    slots = compute_slots(TABLES, [], cfg(opening_hours=hours), now=at(20), first_day=DAY, days=2)
    assert slots[0].start == at(20)
    assert all(s.start.date() == DAY for s in slots)


def test_opening_window_past_midnight():
    c = cfg(opening_hours={"default": DayHours(open=time(20), close=time(2))})
    opens, closes = opening_window(DAY, c)
    assert opens == at(20)
    assert closes == datetime(2030, 1, 8, 2, 0)


def test_yaml_sexagesimal_times_are_understood():
    # yaml.safe_load("open: 18:30") gives 1110
    hours = DayHours.model_validate({"open": 1110, "close": "23:00"})
    assert hours.open == time(18, 30)


def test_pick_tables_best_fit():
    assert [t.id for t in pick_tables(TABLES, [], at(19), 120, 3)] == ["t2"]
    busy = [BusyInterval.from_duration(["t2"], at(18), 120)]
    assert [t.id for t in pick_tables(TABLES, busy, at(19), 120, 3)] == ["t3"]
    assert pick_tables(TABLES, [], at(19), 120, 7) == []


def test_pick_tables_combining():
    chosen = pick_tables(TABLES, [], at(19), 120, 9, allow_combining=True)
    assert sorted(t.id for t in chosen) == ["t2", "t3"]


def test_pick_tables_ignores_superseded_reserve():
    busy = [BusyInterval.from_duration(["t2"], at(19), 120, source_id="old")]
    assert pick_tables(TABLES, busy, at(19), 120, 4)[0].id == "t3"
    assert pick_tables(TABLES, busy, at(19), 120, 4, ignore_source_ids=frozenset({"old"}))[0].id == "t2"
