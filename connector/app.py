"""HTTP API: Ontopo webhooks, health and admin endpoints."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from .config import Settings, load_settings
from .ontopo.client import verify_signature
from .ontopo.mapping import PayloadError
from .service import Connector

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, connector: Connector | None = None, run_background: bool = True) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = connector or Connector(settings)
        app.state.connector = conn
        if not settings.ontopo.webhook_secret:
            log.warning("ontopo.webhook_secret is empty: webhook signatures are NOT verified")
        if run_background:
            conn.start()
        try:
            yield
        finally:
            await conn.stop()

    app = FastAPI(title="Syrve ↔ Ontopo connector", version="0.1.0", lifespan=lifespan)

    def get_connector(request: Request) -> Connector:
        return request.app.state.connector

    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        if settings.server.admin_token and x_admin_token != settings.server.admin_token:
            raise HTTPException(status_code=401, detail="invalid admin token")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/webhooks/ontopo", status_code=202)
    async def ontopo_webhook(request: Request, conn: Connector = Depends(get_connector)) -> dict:
        body = await request.body()
        secret = settings.ontopo.webhook_secret
        if secret and not verify_signature(secret, body, request.headers.get(settings.ontopo.webhook_signature_header)):
            raise HTTPException(status_code=401, detail="invalid signature")
        try:
            payload = json.loads(body)
            event_id = conn.reservations.accept_webhook(payload)
        except (ValueError, PayloadError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        conn.request_inbox_processing()
        return {"accepted": True, "eventId": event_id}

    @app.get("/status", dependencies=[Depends(require_admin)])
    async def status(conn: Connector = Depends(get_connector)) -> dict:
        return conn.status()

    @app.post("/admin/sync/availability", dependencies=[Depends(require_admin)])
    async def sync_availability(conn: Connector = Depends(get_connector)) -> dict:
        pushed = await conn.availability.run(force=True)
        return {"pushed": pushed}

    @app.get("/admin/availability/preview", dependencies=[Depends(require_admin)])
    async def preview_availability(conn: Connector = Depends(get_connector)) -> dict:
        return await conn.availability.build_payload(refresh_tables=True)

    @app.get("/admin/reservations", dependencies=[Depends(require_admin)])
    async def reservations(limit: int = 50, conn: Connector = Depends(get_connector)) -> list[dict]:
        return [asdict(link) for link in conn.store.recent_links(limit)]

    @app.get("/admin/events/failed", dependencies=[Depends(require_admin)])
    async def failed_events(conn: Connector = Depends(get_connector)) -> list[dict]:
        return conn.store.failed_events()

    @app.post("/admin/events/retry", dependencies=[Depends(require_admin)])
    async def retry_events(conn: Connector = Depends(get_connector)) -> dict:
        count = conn.store.retry_failed()
        conn.request_inbox_processing()
        return {"requeued": count}

    return app
