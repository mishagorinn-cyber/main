"""HTTP API: the Ontopo webhook, health and admin endpoints."""

from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from .config import Settings, load_settings
from .ontopo.mapping import PayloadError
from .service import Connector

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, connector: Connector | None = None, run_background: bool = True) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = connector or Connector(settings)
        app.state.connector = conn
        if not settings.ontopo.webhook_token:
            log.warning("ontopo.webhook_token is empty: the webhook accepts requests from anyone")
        if run_background:
            conn.start()
        try:
            yield
        finally:
            await conn.stop()

    app = FastAPI(title="Syrve ↔ Ontopo connector", version="0.2.0", lifespan=lifespan)

    def get_connector(request: Request) -> Connector:
        return request.app.state.connector

    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        if settings.server.admin_token and x_admin_token != settings.server.admin_token:
            raise HTTPException(status_code=401, detail="invalid admin token")

    def check_webhook_token(request: Request) -> None:
        expected = settings.ontopo.webhook_token
        if not expected:
            return
        given = request.query_params.get("token") or request.headers.get("x-api-key") or ""
        if not hmac.compare_digest(given.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="invalid webhook token")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/webhooks/ontopo", dependencies=[Depends(check_webhook_token)])
    async def ontopo_webhook(request: Request, conn: Connector = Depends(get_connector)) -> dict:
        """Receives SyncData from Ontopo. The spec requires an immediate 200: the record is
        only stored here and applied to Syrve by the background worker."""
        try:
            event_id = conn.reservations.accept(json.loads(await request.body()), source="webhook")
        except (ValueError, PayloadError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        conn.request_inbox_processing()
        return {"ok": True, "eventId": event_id}

    @app.get("/status", dependencies=[Depends(require_admin)])
    async def status(conn: Connector = Depends(get_connector)) -> dict:
        return conn.status()

    @app.get("/admin/links", dependencies=[Depends(require_admin)])
    async def links(limit: int = 50, conn: Connector = Depends(get_connector)) -> list[dict]:
        return [link.to_dict() for link in conn.store.recent_links(limit)]

    @app.get("/admin/events/failed", dependencies=[Depends(require_admin)])
    async def failed_events(conn: Connector = Depends(get_connector)) -> list[dict]:
        return conn.store.failed_events()

    @app.post("/admin/events/retry", dependencies=[Depends(require_admin)])
    async def retry_events(conn: Connector = Depends(get_connector)) -> dict:
        count = conn.store.retry_failed()
        conn.request_inbox_processing()
        return {"requeued": count}

    @app.post("/admin/sync/orders", dependencies=[Depends(require_admin)])
    async def sync_orders(conn: Connector = Depends(get_connector)) -> dict:
        return {"sent": await conn.sync_orders_once()}

    @app.post("/admin/sync/reservations", dependencies=[Depends(require_admin)])
    async def sync_reservations(conn: Connector = Depends(get_connector)) -> dict:
        return {"queued": await conn.poll_ontopo_once()}

    return app
