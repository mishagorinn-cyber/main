"""HTTP client for the Ontopo POS API."""

from __future__ import annotations

import logging

import httpx

from ..config import OntopoConfig

log = logging.getLogger(__name__)


class OntopoError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class OntopoClient:
    def __init__(self, config: OntopoConfig, http: httpx.AsyncClient | None = None):
        self.config = config
        headers = {"Accept": "application/json", "x-api-key": config.api_key}
        self._http = http or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"), timeout=config.request_timeout_seconds
        )
        self._http.headers.update(headers)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, body: dict):
        resp = await self._http.post(path, json=body)
        if resp.status_code >= 400:
            raise OntopoError(f"Ontopo API {resp.status_code}: {resp.text[:500]}", resp.status_code)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    async def reservations_for_today(self):
        """POST /pos/reservationsForToday -> list of SyncData."""
        return await self._post("/pos/reservationsForToday", {})

    async def set_reservation(self, sync_data: dict) -> dict | None:
        """POST /pos/setReservation: create (new syncId + order, reservation {}) or update."""
        return await self._post("/pos/setReservation", sync_data)
