import json
from datetime import datetime

import httpx
from fastapi.testclient import TestClient

from connector.app import create_app


def sync(sync_id="S1", status="approved", date="20300107", time="1900", size=3, **reservation):
    body = {
        "reservationId": "ont-" + sync_id,
        "duration": 120,
        "expectedDate": date,
        "expectedTime": time,
        "patron": {"firstName": "Dana", "lastName": "Levi", "phone": "0501234567"},
        "size": size,
        "status": status,
        "table": [],
        "seatingRequests": [],
    }
    body.update(reservation)
    return {"syncId": sync_id, "syncAt": "2030-01-07T10:00:00+02:00", "reservation": body}


async def deliver(connector, payload):
    connector.reservations.accept(payload)
    await connector.reservations.process_due()


def patch_syrve(connector, fake_syrve, monkeypatch, handler):
    monkeypatch.setattr(fake_syrve, "handle", handler)
    connector.syrve._http._transport = fake_syrve.transport()


async def test_create_reserve_on_best_fitting_table(connector, fake_syrve, fake_ontopo):
    await deliver(connector, sync())
    [reserve] = fake_syrve.active()
    body = reserve["body"]
    assert body["tableIds"] == ["t2"]  # 4 seats is the smallest table for 3 guests
    assert body["estimatedStartTime"] == "2030-01-07 19:00:00.000"
    assert body["guests"] == {"count": 3}
    assert body["phone"] == "+972501234567"
    assert body["externalNumber"] == "ont-S1"
    assert body["customer"]["name"] == "Dana" and body["customer"]["surname"] == "Levi"
    link = connector.store.get_link("S1")
    assert link.state == "active" and link.syrve_reserve_id == reserve["id"] and link.origin == "ontopo"
    # The table chosen in Syrve is reported back on the same syncId
    [update] = fake_ontopo.sent
    assert update["syncId"] == "S1" and update["reservation"]["table"] == ["2"]
    assert update["reservation"]["reservationId"] == "ont-S1"


async def test_echo_of_assigned_table_is_noop(connector, fake_syrve, fake_ontopo):
    await deliver(connector, sync())
    await deliver(connector, sync(table=["2"]))  # Ontopo echoes our table assignment
    await deliver(connector, sync(status="seated", table=["2"]))
    assert len(fake_syrve.reserves) == 1
    assert len(fake_ontopo.sent) == 1


async def test_requested_table_is_used(connector, fake_syrve, fake_ontopo):
    await deliver(connector, sync(size=2, table=["3"]))
    assert fake_syrve.active()[0]["tableIds"] == ["t3"]
    assert fake_ontopo.sent == []  # same table as Ontopo asked: nothing to report


async def test_table_map(connector, fake_syrve, settings):
    settings.reservations.table_map["Terrace-1"] = "3"
    await deliver(connector, sync(size=2, table=["Terrace-1"]))
    assert fake_syrve.active()[0]["tableIds"] == ["t3"]


async def test_update_moves_reserve_and_cancels_old(connector, fake_syrve):
    await deliver(connector, sync())
    old_id = connector.store.get_link("S1").syrve_reserve_id
    await deliver(connector, sync(time="2000", table=["2"]))
    active = fake_syrve.active()
    assert len(active) == 1 and active[0]["id"] != old_id
    assert active[0]["estimatedStartTime"] == "2030-01-07 20:00:00.000"
    assert active[0]["tableIds"] == ["t2"]
    assert fake_syrve.reserves[old_id]["cancelled"]


async def test_cancel_and_no_show(connector, fake_syrve):
    await deliver(connector, sync("S1"))
    await deliver(connector, sync("S2", time="1930"))
    await deliver(connector, sync("S1", status="canceled"))
    await deliver(connector, sync("S2", status="noShow"))
    assert fake_syrve.active() == []
    reasons = sorted(r["cancelReason"] for r in fake_syrve.reserves.values())
    assert reasons == ["ClientNotAppeared", "ClientRefused"]
    assert connector.store.get_link("S1").state == "cancelled"


async def test_done_keeps_reserve(connector, fake_syrve):
    await deliver(connector, sync())
    await deliver(connector, sync(status="done", table=["2"]))
    assert len(fake_syrve.active()) == 1
    assert connector.store.get_link("S1").state == "done"


async def test_busy_tables_are_skipped(connector, fake_syrve):
    fake_syrve.add_reserve("existing", ["t2"], datetime(2030, 1, 7, 18, 0))
    await deliver(connector, sync())
    new = [r for r in fake_syrve.active() if r["id"] != "existing"]
    assert new[0]["tableIds"] == ["t3"]


async def test_no_table(connector, fake_syrve):
    await deliver(connector, sync(size=12))
    assert fake_syrve.active() == []
    link = connector.store.get_link("S1")
    assert link.state == "failed" and "no free table" in link.error
    assert connector.store.event_counts() == {"done": 1}


async def test_pos_rejection(connector, fake_syrve):
    fake_syrve.reject_next_create = "Table is locked"
    await deliver(connector, sync())
    link = connector.store.get_link("S1")
    assert link.state == "failed" and "Table is locked" in link.error


async def test_transient_error_is_retried(connector, fake_syrve, monkeypatch):
    original = fake_syrve.handle
    calls = {"n": 0}

    def flaky(request):
        if request.url.path == "/api/1/reserve/create" and calls["n"] == 0:
            calls["n"] += 1
            return httpx.Response(503, json={"errorDescription": "maintenance"})
        return original(request)

    patch_syrve(connector, fake_syrve, monkeypatch, flaky)
    await deliver(connector, sync())
    assert connector.store.event_counts() == {"pending": 1}
    connector.store._exec("UPDATE events SET next_attempt_at = '2000-01-01'")
    await connector.reservations.process_due()
    assert connector.store.event_counts() == {"done": 1}
    assert len(fake_syrve.active()) == 1


async def test_failed_update_keeps_existing_reserve_linked(connector, fake_syrve, settings, monkeypatch):
    settings.reservations.max_attempts = 1
    await deliver(connector, sync(table=["2"]))
    reserve_id = connector.store.get_link("S1").syrve_reserve_id
    original = fake_syrve.handle

    def down(request):
        if request.url.path == "/api/1/reserve/create":
            return httpx.Response(503, json={"errorDescription": "down"})
        return original(request)

    patch_syrve(connector, fake_syrve, monkeypatch, down)
    await deliver(connector, sync(size=4, table=["2"]))
    link = connector.store.get_link("S1")
    assert link.state == "active" and link.syrve_reserve_id == reserve_id
    assert "update not applied" in link.error

    await deliver(connector, sync(status="canceled"))
    assert fake_syrve.reserves[reserve_id]["cancelled"]


async def test_token_refresh_on_401(connector, fake_syrve):
    await connector.syrve.tables()
    fake_syrve.expire_token = True
    tables = await connector.syrve.tables()
    assert [t.id for t in tables] == ["t1", "t2", "t3"]  # deleted tables are skipped
    assert fake_syrve.token_requests == 2


async def test_poll_reservations_for_today(connector, fake_syrve, fake_ontopo):
    fake_ontopo.today = [sync("P1", table=["2"])]
    assert await connector.poll_ontopo_once() == 1
    await connector.reservations.process_due()
    assert connector.store.get_link("P1").state == "active"
    assert await connector.poll_ontopo_once() == 0  # unchanged: not queued again
    assert fake_ontopo.requests[0].headers["x-api-key"] == "key"


def _client(connector, token=""):
    connector.settings.ontopo.webhook_token = token
    return TestClient(create_app(connector.settings, connector=connector, run_background=False))


def test_webhook_returns_200_and_checks_token(connector):
    with _client(connector, token="t0k") as client:
        body = json.dumps(sync()).encode()
        assert client.post("/webhooks/ontopo", content=body).status_code == 401
        assert client.post("/webhooks/ontopo?token=bad", content=body).status_code == 401
        ok = client.post("/webhooks/ontopo?token=t0k", content=body)
        assert ok.status_code == 200 and ok.json()["ok"] is True
        assert client.post("/webhooks/ontopo", content=body, headers={"x-api-key": "t0k"}).status_code == 200
        assert connector.store.event_counts() == {"pending": 2}


def test_webhook_rejects_bad_payload_and_admin_token(connector):
    connector.settings.server.admin_token = "adm"
    with _client(connector) as client:
        assert client.post("/webhooks/ontopo", content=b"not json").status_code == 400
        assert client.post("/webhooks/ontopo", json={"reservation": {}}).status_code == 400
        assert client.get("/status").status_code == 401
        assert client.get("/status", headers={"X-Admin-Token": "adm"}).json()["events"] == {}
        assert client.get("/health").json() == {"status": "ok"}
