"""Wires clients and sync engines together and runs the background loops."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .config import Settings
from .ontopo.client import OntopoClient
from .ontopo.mapping import PayloadError, parse_sync_list
from .orders import OrderSync
from .reservations import ReservationSync
from .storage import Store
from .syrve.client import SyrveClient
from .tables import TableDirectory

log = logging.getLogger(__name__)

INBOX_POLL_SECONDS = 5


class Connector:
    def __init__(self, settings: Settings, syrve=None, ontopo=None, store: Store | None = None):
        self.settings = settings
        self.syrve = syrve or SyrveClient(settings.syrve)
        self.ontopo = ontopo or OntopoClient(settings.ontopo)
        self.store = store or Store(settings.storage.path)
        self.directory = TableDirectory(settings, self.syrve)
        self.reservations = ReservationSync(settings, self.syrve, self.ontopo, self.store, self.directory)
        self.orders = OrderSync(settings, self.syrve, self.ontopo, self.store, self.directory)
        self._inbox_wakeup = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.errors: dict[str, str | None] = {"orders": None, "poll": None}

    def request_inbox_processing(self) -> None:
        self._inbox_wakeup.set()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._inbox_loop(), name="reservation-inbox")]
        if self.settings.orders.enabled:
            self._tasks.append(asyncio.create_task(self._orders_loop(), name="order-sync"))
        if self.settings.ontopo.poll_reservations:
            self._tasks.append(asyncio.create_task(self._poll_loop(), name="ontopo-poll"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self.syrve.aclose()
        await self.ontopo.aclose()
        self.store.close()

    async def _inbox_loop(self) -> None:
        while True:
            try:
                await self.reservations.process_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Reservation inbox processing failed")
            try:
                await asyncio.wait_for(self._inbox_wakeup.wait(), timeout=INBOX_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._inbox_wakeup.clear()

    async def _orders_loop(self) -> None:
        await self._periodic("orders", self.sync_orders_once, self.settings.orders.sync_interval_seconds)

    async def _poll_loop(self) -> None:
        await self._periodic("poll", self.poll_ontopo_once, self.settings.ontopo.poll_interval_seconds)

    async def _periodic(self, name: str, job, interval: float) -> None:
        while True:
            try:
                await job()
                self.errors[name] = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors[name] = str(exc)
                log.exception("%s job failed", name)
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------ jobs

    async def sync_orders_once(self) -> int:
        sent = await self.orders.run()
        self.store.set_meta("orders_synced_at", datetime.now(timezone.utc).isoformat())
        return sent

    async def poll_ontopo_once(self) -> int:
        """Re-read today's reservations from Ontopo; unchanged ones are no-ops."""
        items = parse_sync_list(await self.ontopo.reservations_for_today())
        accepted = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            link = self.store.get_link(str(item.get("syncId") or ""))
            if link and link.ontopo_reservation == (item.get("reservation") or {}):
                continue  # nothing new since the last webhook/poll
            try:
                self.reservations.accept(item, source="poll")
                accepted += 1
            except PayloadError as exc:
                log.warning("Skipping reservation from reservationsForToday: %s", exc)
        self.store.set_meta("ontopo_polled_at", datetime.now(timezone.utc).isoformat())
        self.store.purge_done_events(older_than_days=7)
        if accepted:
            self.request_inbox_processing()
        return accepted

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        return {
            "orders": {"lastSyncAt": self.store.get_meta("orders_synced_at"), "lastError": self.errors["orders"]},
            "ontopoPoll": {"lastPollAt": self.store.get_meta("ontopo_polled_at"), "lastError": self.errors["poll"]},
            "events": self.store.event_counts(),
        }
