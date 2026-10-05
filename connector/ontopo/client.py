"""HTTP client for the Ontopo partner API (paths and auth come from config)."""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime

import httpx

from ..config import OntopoConfig

log = logging.getLogger(__name__)


class OntopoError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256 of the raw body, hex-encoded; an optional "sha256=" prefix is allowed."""
    if not signature:
        return False
    signature = signature.strip()
    if signature.startswith("sha256="):
        signature = signature[len("sha256="):]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


class OntopoClient:
    def __init__(self, config: OntopoConfig, http: httpx.AsyncClient | None = None):
        self.config = config
        headers = {"Accept": "application/json"}
        if config.api_key:
            prefix = f"{config.auth_scheme} " if config.auth_scheme else ""
            headers[config.auth_header] = prefix + config.api_key
        self._http = http or httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"), timeout=config.request_timeout_seconds, headers=headers
        )
        if http is not None:
            self._http.headers.update(headers)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _path(self, template: str, **extra: str) -> str:
        return template.format(venue_id=self.config.venue_id, **extra)

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            raise OntopoError(f"Ontopo API {resp.status_code}: {resp.text[:500]}", resp.status_code)

    async def push_availability(self, payload: dict) -> None:
        resp = await self._http.put(self._path(self.config.availability_path), json=payload)
        self._check(resp)

    async def list_reservations(self, updated_since: datetime | None = None) -> object:
        params = {}
        if updated_since is not None:
            params["updatedSince"] = updated_since.isoformat()
        resp = await self._http.get(self._path(self.config.reservations_path), params=params)
        self._check(resp)
        return resp.json()

    async def report_status(self, reservation_id: str, payload: dict) -> None:
        if not self.config.reservation_status_path:
            return
        path = self._path(self.config.reservation_status_path, reservation_id=reservation_id)
        resp = await self._http.post(path, json=payload)
        self._check(resp)
