"""Client for the Syrve Cloud API (https://api-eu.syrve.live, iikoCloud-compatible).

Only the endpoints needed for table reservations are wrapped:

* POST /api/1/access_token
* POST /api/1/organizations
* POST /api/1/reserve/available_terminal_groups
* POST /api/1/reserve/available_restaurant_sections
* POST /api/1/reserve/restaurant_sections_workload
* POST /api/1/reserve/create
* POST /api/1/reserve/cancel
* POST /api/1/reserve/status_by_id
* POST /api/1/order/by_table            (orders on tables -> table occupancy in Ontopo)

Syrve exchanges local restaurant time as "yyyy-MM-dd HH:mm:ss.fff".
"""

from __future__ import annotations

import asyncio
import logging
import time as monotonic_time
from datetime import datetime
from typing import Any

import httpx

from ..config import SyrveConfig
from ..models import BusyInterval, Table

log = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 45 * 60  # Syrve tokens live ~60 min; refresh early.


class SyrveError(Exception):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class SyrveReserveFailed(SyrveError):
    """The POS rejected the reserve (creationStatus == Error)."""


def format_syrve_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.") + f"{value.microsecond // 1000:03d}"


def parse_syrve_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.strip().replace("T", " "))
    # Syrve sends local time; drop an offset if one is ever present.
    return dt.replace(tzinfo=None)


class SyrveClient:
    def __init__(self, config: SyrveConfig, http: httpx.AsyncClient | None = None):
        self.config = config
        self._http = http or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"), timeout=config.request_timeout_seconds
        )
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ transport

    async def _get_token(self, force: bool = False) -> str:
        async with self._token_lock:
            if not force and self._token and monotonic_time.monotonic() < self._token_expires:
                return self._token
            resp = await self._http.post("/api/1/access_token", json={"apiLogin": self.config.api_login})
            data = self._decode(resp)
            token = data.get("token")
            if not token:
                raise SyrveError("Syrve did not return an access token", resp.status_code, data)
            self._token = token
            self._token_expires = monotonic_time.monotonic() + TOKEN_TTL_SECONDS
            return token

    async def _post(self, path: str, body: dict) -> dict:
        token = await self._get_token()
        resp = await self._http.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 401:
            token = await self._get_token(force=True)
            resp = await self._http.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
        return self._decode(resp)

    @staticmethod
    def _decode(resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        if resp.status_code >= 400:
            description = data.get("errorDescription") or data.get("error") or resp.text
            raise SyrveError(f"Syrve API {resp.status_code}: {description}", resp.status_code, data)
        return data

    # ------------------------------------------------------------------ discovery

    async def organizations(self) -> list[dict]:
        data = await self._post("/api/1/organizations", {"returnAdditionalInfo": False, "includeDisabled": False})
        return data.get("organizations", [])

    async def terminal_groups(self, organization_ids: list[str] | None = None) -> list[dict]:
        data = await self._post(
            "/api/1/reserve/available_terminal_groups",
            {"organizationIds": organization_ids or [self.config.organization_id]},
        )
        groups: list[dict] = []
        for org in data.get("terminalGroups", []):
            groups.extend(org.get("items", []))
        return groups

    async def restaurant_sections(self, terminal_group_ids: list[str] | None = None) -> list[dict]:
        data = await self._post(
            "/api/1/reserve/available_restaurant_sections",
            {
                "terminalGroupIds": terminal_group_ids or [self.config.terminal_group_id],
                "returnSchema": False,
            },
        )
        return data.get("restaurantSections", [])

    async def tables(self) -> list[Table]:
        wanted = set(self.config.restaurant_section_ids)
        result: list[Table] = []
        for section in await self.restaurant_sections():
            if wanted and section.get("id") not in wanted:
                continue
            for t in section.get("tables", []):
                if t.get("isDeleted"):
                    continue
                result.append(
                    Table(
                        id=t["id"],
                        number=t.get("number"),
                        name=t.get("name") or str(t.get("number", "")),
                        capacity=int(t.get("seatingCapacity") or 0),
                        section_id=section["id"],
                        section_name=section.get("name", ""),
                    )
                )
        return result

    # ------------------------------------------------------------------ occupancy

    async def workload(self, section_ids: list[str], date_from: datetime, date_to: datetime) -> list[BusyInterval]:
        """Existing reserves/banquets on the given sections between two local datetimes."""
        data = await self._post(
            "/api/1/reserve/restaurant_sections_workload",
            {
                "restaurantSectionIds": section_ids,
                "dateFrom": format_syrve_time(date_from),
                "dateTo": format_syrve_time(date_to),
            },
        )
        intervals = []
        for r in data.get("reserves", []):
            if not r.get("tableIds") or not r.get("estimatedStartTime"):
                continue
            intervals.append(
                BusyInterval.from_duration(
                    r["tableIds"],
                    parse_syrve_time(r["estimatedStartTime"]),
                    int(r.get("durationInMinutes") or 0),
                    source_id=r.get("id", ""),
                )
            )
        return intervals

    async def table_orders(
        self, table_ids: list[str], date_from: datetime, statuses: list[str] | None = None
    ) -> list[dict]:
        """Orders on tables since date_from. Each item is the order dict with the order "id" filled in."""
        body: dict[str, Any] = {
            "organizationIds": [self.config.organization_id],
            "tableIds": table_ids,
            "dateFrom": format_syrve_time(date_from),
        }
        if statuses:
            body["statuses"] = statuses
        data = await self._post("/api/1/order/by_table", body)
        result = []
        for wrapper in data.get("orders", []):
            order = wrapper.get("order")
            if not order:  # creation still in progress or failed
                continue
            result.append({**order, "id": wrapper.get("id") or order.get("id")})
        return result

    # ------------------------------------------------------------------ reserves

    async def create_reserve(
        self,
        *,
        reserve_id: str,
        external_number: str,
        start: datetime,
        duration_minutes: int,
        table_ids: list[str],
        guests_count: int,
        name: str,
        surname: str = "",
        phone: str = "",
        email: str = "",
        comment: str = "",
    ) -> dict:
        customer: dict[str, Any] = {"type": self.config.customer_type, "name": name or "Ontopo guest"}
        if surname:
            customer["surname"] = surname
        if email:
            customer["email"] = email
        body: dict[str, Any] = {
            "organizationId": self.config.organization_id,
            "terminalGroupId": self.config.terminal_group_id,
            "id": reserve_id,
            "externalNumber": external_number,
            "customer": customer,
            "guests": {"count": guests_count},
            "comment": comment,
            "durationInMinutes": duration_minutes,
            "shouldRemind": self.config.should_remind,
            "tableIds": table_ids,
            "estimatedStartTime": format_syrve_time(start),
        }
        if phone:
            body["phone"] = phone
        data = await self._post("/api/1/reserve/create", body)
        return data.get("reserveInfo") or data

    async def reserve_statuses(self, reserve_ids: list[str]) -> list[dict]:
        data = await self._post(
            "/api/1/reserve/status_by_id",
            {"organizationId": self.config.organization_id, "reserveIds": reserve_ids},
        )
        return data.get("reserves") or data.get("reserveInfos") or []

    async def wait_reserve_created(self, reserve_id: str) -> dict:
        """Poll until the POS confirms the reserve. Raises SyrveReserveFailed on Error."""
        info: dict = {}
        for attempt in range(self.config.creation_poll_attempts):
            statuses = await self.reserve_statuses([reserve_id])
            info = next((s for s in statuses if s.get("id") == reserve_id), {})
            status = info.get("creationStatus")
            if status == "Success":
                return info
            if status == "Error":
                error = info.get("errorInfo") or {}
                message = error.get("message") or error.get("description") or error.get("code") or "unknown error"
                raise SyrveReserveFailed(f"Syrve rejected reserve: {message}", payload=info)
            if attempt + 1 < self.config.creation_poll_attempts:
                await asyncio.sleep(self.config.creation_poll_interval_seconds)
        # Still InProgress: the terminal may be offline. Syrve will deliver it later.
        log.warning("Reserve %s still in progress after polling", reserve_id)
        return info

    async def cancel_reserve(self, reserve_id: str, reason: str = "ClientRefused") -> None:
        await self._post(
            "/api/1/reserve/cancel",
            {"organizationId": self.config.organization_id, "reserveId": reserve_id, "cancelReason": reason},
        )
