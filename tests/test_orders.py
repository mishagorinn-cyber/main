from datetime import datetime

from .test_sync import deliver, sync

ITEMS = [
    {"type": "Product", "product": {"id": "p1", "name": "Hummus"}, "amount": 2, "price": 32.0},
    {"type": "Product", "product": {"id": "p2", "name": "Beer"}, "amount": 1, "price": 28.5, "deleted": True},
]


async def test_order_on_reserved_table_seats_then_completes_reservation(connector, fake_syrve, fake_ontopo):
    await deliver(connector, sync(table=["2"]))  # reserve 19:00 on table 2
    fake_ontopo.sent.clear()

    order = fake_syrve.add_order("o1", ["t2"], datetime(2030, 1, 7, 18, 50), items=ITEMS, sum=64, number=17)
    assert await connector.orders.run() == 1
    [seated] = fake_ontopo.sent
    assert seated["syncId"] == "S1"
    assert seated["reservation"]["status"] == "seated"
    assert seated["reservation"]["reservationId"] == "ont-S1"
    assert seated["order"] == {
        "posOrderId": "o1",
        "orderNumber": 17,
        "openedAt": "2030-01-07T18:50:00+02:00",
        "netTotal": 64,
        "discountAmount": 0,
        "dinnersCount": 2,
        "stage": "ordered",
        "tables": [2],
        "items": [{"itemId": "p1", "itemName": "Hummus", "quantity": 2, "price": 32}],
    }

    assert await connector.orders.run() == 0  # nothing changed

    order["status"] = "Bill"
    assert await connector.orders.run() == 1
    assert fake_ontopo.sent[-1]["order"]["stage"] == "check"

    order["status"] = "Closed"
    assert await connector.orders.run() == 1
    done = fake_ontopo.sent[-1]
    assert done["reservation"]["status"] == "done" and done["order"]["stage"] == "paid"
    assert connector.store.get_link("S1").state == "done"
    assert await connector.orders.run() == 0


async def test_walk_in_is_created_in_ontopo(connector, fake_syrve, fake_ontopo):
    order = fake_syrve.add_order("w1", ["t1"], datetime(2030, 1, 7, 13, 0))
    assert await connector.orders.run() == 1
    [created] = fake_ontopo.sent
    assert created["syncId"] == "syrve-w1"
    assert created["reservation"] == {}  # spec: POS-origin create
    assert created["order"]["tables"] == [1] and "stage" not in created["order"]
    link = connector.store.get_link("syrve-w1")
    assert link.origin == "pos" and link.ontopo_reservation_id == "ont-syrve-w1"

    # Ontopo's webhook echo for our walk-in must not create a Syrve reserve
    await deliver(connector, {"syncId": "syrve-w1", "syncAt": "x", "reservation": {
        "reservationId": "ont-syrve-w1", "expectedDate": "20300107", "expectedTime": "1300",
        "size": 2, "status": "seated", "table": ["1"]}})
    assert fake_syrve.active() == []

    order["status"] = "Closed"
    assert await connector.orders.run() == 1
    closed = fake_ontopo.sent[-1]
    assert closed["reservation"]["status"] == "done"
    assert closed["reservation"]["reservationId"] == "ont-syrve-w1"


async def test_order_far_from_reservation_time_is_a_walk_in(connector, fake_syrve, fake_ontopo):
    await deliver(connector, sync(table=["2"]))  # 19:00-21:00
    fake_syrve.add_order("o2", ["t2"], datetime(2030, 1, 7, 13, 0))
    await connector.orders.run()
    assert fake_ontopo.sent[-1]["syncId"] == "syrve-o2"


async def test_orders_closed_before_first_sight_are_ignored(connector, fake_syrve, fake_ontopo):
    fake_syrve.add_order("old", ["t1"], datetime(2030, 1, 7, 12, 0), status="Closed")
    assert await connector.orders.run() == 0
    assert fake_ontopo.sent == []


async def test_walk_ins_can_be_disabled(connector, fake_syrve, fake_ontopo, settings):
    settings.orders.create_walk_ins = False
    fake_syrve.add_order("w1", ["t1"], datetime(2030, 1, 7, 13, 0))
    assert await connector.orders.run() == 0


async def test_seated_update_keeps_table_assigned_by_syrve(connector, fake_syrve, fake_ontopo):
    await deliver(connector, sync())  # Ontopo gave no table; Syrve picks table 2
    fake_syrve.add_order("o1", ["t2"], datetime(2030, 1, 7, 19, 5))
    await connector.orders.run()
    assert fake_ontopo.sent[-1]["reservation"]["table"] == ["2"]
