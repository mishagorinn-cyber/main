import hashlib
import hmac
import json
from datetime import datetime

from fastapi.testclient import TestClient

from connector.app import create_app

from .fakes import make_settings

START = "2030-01-07T19:00:00+02:00"


def webhook(res_id="R1", event="reservation.created", start=START, party=3, **extra):
    reservation = {
        "id": res_id,
        "status": extra.pop("status", "confirmed"),
        "start": start,
        "partySize": party,
        "guest": {"firstName": "Dana", "lastName": "Levi", "phone": "0501234567", "email": "d@example.com"},
    }
    reservation.update(extra)
    return {"event": event, "reservation": reservation}


async def deliver(connector, payload):
    connector.reservations.accept_webhook(payload)
    await connector.reservations.process_due()


async def test_create_reserve_on_best_fitting_table(connector, fake_syrve, fake_ontopo):
    await deliver(connector, webhook())
    [reserve] = fake_syrve.active()
    body = reserve["body"]
    assert body["tableIds"] == ["t2"]  # 4 seats is the smallest table for 3 guests
    assert body["estimatedStartTime"] == "2030-01-07 19:00:00.000"
    assert body["guests"] == {"count": 3}
    assert body["phone"] == "+972501234567"
    assert body["externalNumber"] == "ontopo-R1"
    assert body["organizationId"] == "org-1" and body["terminalGroupId"] == "tg-1"
    link = connector.store.get_link("R1")
    assert link.state == "active" and link.syrve_reserve_id == reserve["id"]
    assert fake_ontopo.statuses[-1]["status"] == "confirmed"
    assert fake_ontopo.statuses[-1]["tables"][0]["id"] == "t2"


async def test_duplicate_webhook_is_noop(connector, fake_syrve):
    await deliver(connector, webhook())
    await deliver(connector, webhook(event="reservation.updated"))
    assert len(fake_syrve.reserves) == 1


async def test_update_moves_reserve_and_cancels_old(connector, fake_syrve):
    await deliver(connector, webhook())
    old_id = connector.store.get_link("R1").syrve_reserve_id
    await deliver(connector, webhook(event="reservation.updated", start="2030-01-07T20:00:00+02:00"))
    active = fake_syrve.active()
    assert len(active) == 1 and active[0]["id"] != old_id
    assert active[0]["estimatedStartTime"] == "2030-01-07 20:00:00.000"
    assert active[0]["tableIds"] == ["t2"]  # keeps the same table when still free
    assert fake_syrve.reserves[old_id]["cancelled"]


async def test_cancel(connector, fake_syrve):
    await deliver(connector, webhook())
    await deliver(connector, webhook(event="reservation.cancelled", status="cancelled"))
    assert fake_syrve.active() == []
    assert connector.store.get_link("R1").state == "cancelled"
    reserve = next(iter(fake_syrve.reserves.values()))
    assert reserve["cancelReason"] == "ClientRefused"


async def test_busy_tables_are_skipped(connector, fake_syrve):
    fake_syrve.add_reserve("existing", ["t2"], datetime(2030, 1, 7, 18, 0))
    await deliver(connector, webhook())
    new = [r for r in fake_syrve.active() if r["id"] != "existing"]
    assert new[0]["tableIds"] == ["t3"]


async def test_requested_table_mapping(connector, fake_syrve, settings):
    settings.reservations.table_map["ontopo-A"] = "t3"
    await deliver(connector, webhook(party=2, tableIds=["ontopo-A"]))
    assert fake_syrve.active()[0]["tableIds"] == ["t3"]


async def test_no_table_rejects_and_reports(connector, fake_syrve, fake_ontopo):
    await deliver(connector, webhook(party=12))
    assert fake_syrve.active() == []
    assert connector.store.get_link("R1").state == "failed"
    assert fake_ontopo.statuses[-1]["status"] == "rejected"
    assert connector.store.event_counts() == {"done": 1}


async def test_pos_rejection(connector, fake_syrve, fake_ontopo):
    fake_syrve.reject_next_create = "Table is locked"
    await deliver(connector, webhook())
    link = connector.store.get_link("R1")
    assert link.state == "failed" and "Table is locked" in link.error
    assert fake_ontopo.statuses[-1]["status"] == "rejected"


async def test_transient_error_is_retried(connector, fake_syrve, monkeypatch):
    calls = {"n": 0}
    original = fake_syrve.handle

    def flaky(request):
        if request.url.path == "/api/1/reserve/create" and calls["n"] == 0:
            calls["n"] += 1
            import httpx

            return httpx.Response(503, json={"errorDescription": "maintenance"})
        return original(request)

    monkeypatch.setattr(fake_syrve, "handle", flaky)
    connector.syrve._http._transport = fake_syrve.transport()
    await deliver(connector, webhook())
    assert connector.store.event_counts() == {"pending": 1}
    connector.store._exec("UPDATE events SET next_attempt_at = '2000-01-01'")
    await connector.reservations.process_due()
    assert connector.store.event_counts() == {"done": 1}
    assert len(fake_syrve.active()) == 1


async def test_token_refresh_on_401(connector, fake_syrve):
    await connector.syrve.tables()
    fake_syrve.expire_token = True
    tables = await connector.syrve.tables()
    assert [t.id for t in tables] == ["t1", "t2", "t3"]  # deleted tables are skipped
    assert fake_syrve.token_requests == 2


async def test_availability_push_and_dedup(connector, fake_syrve, fake_ontopo, monkeypatch):
    monkeypatch.setattr(connector.availability, "now", lambda: datetime(2030, 1, 7, 12, 0))
    fake_syrve.add_reserve("existing", ["t2"], datetime(2030, 1, 7, 19, 0))
    assert await connector.availability.run() is True
    assert await connector.availability.run() is False
    payload = fake_ontopo.availability[-1]
    assert payload["venueId"] == "venue-1"
    assert {t["id"] for t in payload["tables"]} == {"t1", "t2", "t3"}
    slot = next(s for s in payload["slots"] if s["start"] == "2030-01-07T19:00:00+02:00")
    assert slot["freeTableIds"] == ["t1", "t3"]
    assert slot["maxPartySize"] == 6 and slot["freeSeats"] == 8
    assert fake_ontopo.requests[0].headers["Authorization"] == "Bearer key"

    await deliver(connector, webhook(party=6))  # takes t3 at 19:00
    assert await connector.availability.run() is True
    slot = next(s for s in fake_ontopo.availability[-1]["slots"] if s["start"] == "2030-01-07T19:00:00+02:00")
    assert slot["freeTableIds"] == ["t1"]


async def test_poll_ontopo(connector, fake_syrve, fake_ontopo):
    fake_ontopo.reservations = [webhook("P1")["reservation"]]
    assert await connector.poll_ontopo_once() == 1
    await connector.reservations.process_due()
    assert connector.store.get_link("P1").state == "active"


def _client(connector, secret=""):
    settings = connector.settings
    settings.ontopo.webhook_secret = secret
    app = create_app(settings, connector=connector, run_background=False)
    return TestClient(app)


def test_webhook_signature(connector):
    with _client(connector, secret="s3cret") as client:
        body = json.dumps(webhook()).encode()
        assert client.post("/webhooks/ontopo", content=body).status_code == 401
        bad = client.post("/webhooks/ontopo", content=body, headers={"X-Ontopo-Signature": "sha256=00"})
        assert bad.status_code == 401
        sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        ok = client.post("/webhooks/ontopo", content=body, headers={"X-Ontopo-Signature": f"sha256={sig}"})
        assert ok.status_code == 202
        assert connector.store.event_counts() == {"pending": 1}


def test_webhook_rejects_bad_payload_and_admin_token(connector):
    connector.settings.server.admin_token = "adm"
    with _client(connector) as client:
        assert client.post("/webhooks/ontopo", content=b"not json").status_code == 400
        assert client.post("/webhooks/ontopo", json={"event": "x"}).status_code == 400
        assert client.get("/status").status_code == 401
        assert client.get("/status", headers={"X-Admin-Token": "adm"}).json()["events"] == {}
        assert client.get("/health").json() == {"status": "ok"}


async def test_failed_update_keeps_existing_reserve_linked(connector, fake_syrve, settings, monkeypatch):
    settings.reservations.max_attempts = 1
    await deliver(connector, webhook())
    reserve_id = connector.store.get_link("R1").syrve_reserve_id
    original = fake_syrve.handle

    def down(request):
        import httpx

        if request.url.path == "/api/1/reserve/create":
            return httpx.Response(503, json={"errorDescription": "down"})
        return original(request)

    monkeypatch.setattr(fake_syrve, "handle", down)
    connector.syrve._http._transport = fake_syrve.transport()
    await deliver(connector, webhook(event="reservation.updated", party=4))
    link = connector.store.get_link("R1")
    assert link.state == "active" and link.syrve_reserve_id == reserve_id
    assert "update not applied" in link.error

    await deliver(connector, webhook(event="reservation.cancelled", status="cancelled"))
    assert fake_syrve.reserves[reserve_id]["cancelled"]
