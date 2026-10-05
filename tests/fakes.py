"""In-memory fakes of the Syrve Cloud API and the Ontopo partner API (httpx MockTransport)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx

from connector.config import Settings

SECTION_ID = "sec-main"
TABLES = [
    {"id": "t1", "number": 1, "name": "1", "seatingCapacity": 2, "isDeleted": False},
    {"id": "t2", "number": 2, "name": "2", "seatingCapacity": 4, "isDeleted": False},
    {"id": "t3", "number": 3, "name": "3", "seatingCapacity": 6, "isDeleted": False},
    {"id": "t9", "number": 9, "name": "old", "seatingCapacity": 4, "isDeleted": True},
]


def make_settings(**overrides) -> Settings:
    raw = {
        "timezone": "Asia/Jerusalem",
        "syrve": {
            "base_url": "https://syrve.test",
            "api_login": "login",
            "organization_id": "org-1",
            "terminal_group_id": "tg-1",
            "creation_poll_interval_seconds": 0,
        },
        "ontopo": {"base_url": "https://ontopo.test", "api_key": "key"},
        "reservations": {"buffer_minutes": 0},
        "storage": {"path": ":memory:"},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key].update(value)
        else:
            raw[key] = value
    return Settings.model_validate(raw)


class FakeSyrve:
    def __init__(self):
        self.reserves: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.token_requests = 0
        self.reject_next_create: str | None = None
        self.expire_token = False
        self.orders: dict[str, dict] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def add_reserve(self, reserve_id: str, table_ids: list[str], start: datetime, minutes: int = 120) -> None:
        self.reserves[reserve_id] = {
            "id": reserve_id,
            "tableIds": table_ids,
            "estimatedStartTime": start.strftime("%Y-%m-%d %H:%M:%S.000"),
            "durationInMinutes": minutes,
            "status": "Success",
            "cancelled": False,
        }

    def add_order(self, order_id: str, table_ids: list[str], opened: datetime, status: str = "New", **extra) -> dict:
        order = {
            "tableIds": table_ids,
            "status": status,
            "whenCreated": opened.strftime("%Y-%m-%d %H:%M:%S.000"),
            "number": len(self.orders) + 100,
            "sum": 0,
            "guestsInfo": {"count": 2},
            "items": [],
        }
        order.update(extra)
        self.orders[order_id] = order
        return order

    def active(self) -> list[dict]:
        return [r for r in self.reserves.values() if not r["cancelled"]]

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        path = request.url.path
        self.calls.append((path, body))
        if path == "/api/1/access_token":
            self.token_requests += 1
            return httpx.Response(200, json={"token": f"tok-{self.token_requests}"})
        if request.headers.get("Authorization") != f"Bearer tok-{self.token_requests}" or self.expire_token:
            self.expire_token = False
            return httpx.Response(401, json={"errorDescription": "token expired"})
        if path == "/api/1/reserve/available_restaurant_sections":
            return httpx.Response(
                200,
                json={"restaurantSections": [{"id": SECTION_ID, "name": "Main hall", "tables": TABLES}]},
            )
        if path == "/api/1/reserve/restaurant_sections_workload":
            date_from = datetime.fromisoformat(body["dateFrom"])
            date_to = datetime.fromisoformat(body["dateTo"])
            out = []
            for r in self.active():
                start = datetime.fromisoformat(r["estimatedStartTime"])
                end = start + timedelta(minutes=r["durationInMinutes"])
                if start < date_to and end > date_from:
                    out.append({k: r[k] for k in ("id", "tableIds", "estimatedStartTime", "durationInMinutes")})
            return httpx.Response(200, json={"reserves": out})
        if path == "/api/1/reserve/create":
            status = "Error" if self.reject_next_create else "Success"
            self.reserves[body["id"]] = {
                "id": body["id"],
                "tableIds": body["tableIds"],
                "estimatedStartTime": body["estimatedStartTime"],
                "durationInMinutes": body["durationInMinutes"],
                "status": status,
                "error": self.reject_next_create,
                "cancelled": status == "Error",
                "body": body,
            }
            self.reject_next_create = None
            return httpx.Response(200, json={"reserveInfo": {"id": body["id"], "creationStatus": "InProgress"}})
        if path == "/api/1/reserve/status_by_id":
            out = []
            for rid in body["reserveIds"]:
                r = self.reserves.get(rid)
                if r:
                    info = {"id": rid, "creationStatus": r["status"]}
                    if r["status"] == "Error":
                        info["errorInfo"] = {"message": r["error"]}
                    out.append(info)
            return httpx.Response(200, json={"reserves": out})
        if path == "/api/1/order/by_table":
            out = [
                {"id": oid, "creationStatus": "Success", "order": dict(o)}
                for oid, o in self.orders.items()
                if set(o["tableIds"]) & set(body["tableIds"]) and o["status"] in body.get("statuses", [o["status"]])
            ]
            return httpx.Response(200, json={"orders": out})
        if path == "/api/1/reserve/cancel":
            r = self.reserves.get(body["reserveId"])
            if not r:
                return httpx.Response(400, json={"errorDescription": "reserve not found"})
            r["cancelled"] = True
            r["cancelReason"] = body["cancelReason"]
            return httpx.Response(200, json={"correlationId": "c"})
        return httpx.Response(404, json={"errorDescription": f"unknown path {path}"})


class FakeOntopo:
    """Ontopo POS API: /pos/setReservation and /pos/reservationsForToday."""

    def __init__(self):
        self.sent: list[dict] = []  # SyncData bodies received on /pos/setReservation
        self.today: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.fail_set_reservation = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("x-api-key") != "key":
            return httpx.Response(401, json={"error": "bad key"})
        path = request.url.path
        if request.method == "POST" and path == "/pos/setReservation":
            if self.fail_set_reservation:
                return httpx.Response(500, json={"error": "boom"})
            body = json.loads(request.content)
            self.sent.append(body)
            reservation = dict(body.get("reservation") or {})
            if not reservation.get("reservationId"):
                reservation["reservationId"] = "ont-" + body["syncId"]
            return httpx.Response(200, json={**body, "reservation": reservation})
        if request.method == "POST" and path == "/pos/reservationsForToday":
            return httpx.Response(200, json=self.today)
        return httpx.Response(404)
