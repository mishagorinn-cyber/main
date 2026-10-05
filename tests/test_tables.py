from datetime import date, datetime, time

from connector.models import BusyInterval, Table
from connector.tables import TableDirectory, pick_tables

from .fakes import make_settings

TABLES = [
    Table("t1", 1, "1", 2, "s1"),
    Table("t2", 2, "2", 4, "s1"),
    Table("t3", 3, "3", 6, "s1", ),
]
DAY = date(2030, 1, 7)


def at(h, m=0, day=DAY):
    return datetime.combine(day, time(h, m))


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


def test_table_labels():
    settings = make_settings(reservations={"table_map": {"VIP": "t3"}})
    directory = TableDirectory(settings, syrve=None)
    directory._tables = TABLES
    assert directory.resolve_label("2").id == "t2"      # by Syrve table number
    assert directory.resolve_label("VIP").id == "t3"    # via table_map
    assert directory.resolve_label("99") is None
    assert directory.label_for(TABLES[2]) == "VIP"
    assert directory.label_for(TABLES[0]) == "1"
