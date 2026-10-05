"""Wires clients and sync engines together and runs the background loops."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .availability import AvailabilitySync
from .config import Settings
from .ontopo.client import OntopoClient
from .ontopo.mapping import parse_reservation_list
from .reservations import ReservationSync
from .storage import Store
from .syrve.client import SyrveClient

log = logging.getLogger(__name__)

INBOX_POLL_SECONDS = 5


class Connector:
    def __init__(self, settings: Settings, syrve=None, ontopo=None, store: Store | None = None):
        self.settings = settings
        self.syrve = syrve or SyrveClient(settings.syrve)
        self.ontopo = ontopo or OntopoClient(settings.ontopo)
        self.store = store or Store(settings.storage.path)
        self.availability = AvailabilitySync(settings, self.syrve, self.ontopo, self.store)
        self.reservations = ReservationSync(
            settings,
            self.syrve,
            self.ontopo,
            self.store,
            tables_loader=self.availability.load_tables,
            on_change=self.request_availability_sync,
        )
        self._availability_wakeup = asyncio.Event()
        self._inbox_wakeup = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.last_availability_error: str | None = None

    # ------------------------------------------------------------------ signals

    def request_availability_sync(self) -> None:
        self._availability_wakeup.set()

    def request_inbox_processing(self) -> None:
        self._inbox_wakeup.set()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._availability_loop(), name="availability-sync"),
            asyncio.create_task(self._inbox_loop(), name="reservation-inbox"),
        ]
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

    @staticmethod
    async def _wait(event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        event.clear()

    async def _availability_loop(self) -> None:
        interval = self.settings.availability.sync_interval_seconds
        while True:
            try:
                await self.availability.run()
                self.last_availability_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_availability_error = str(exc)
                log.exception("Availability sync failed")
            await self._wait(self._availability_wakeup, interval)

    async def _inbox_loop(self) -> None:
        while True:
            try:
                await self.reservations.process_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Reservation inbox processing failed")
            await self._wait(self._inbox_wakeup, INBOX_POLL_SECONDS)

    async def _poll_loop(self) -> None:
        interval = self.settings.ontopo.poll_interval_seconds
        while True:
            try:
                await self.poll_ontopo_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Polling Ontopo reservations failed")
            await asyncio.sleep(interval)

    async def poll_ontopo_once(self) -> int:
        cursor_raw = self.store.get_meta("ontopo_poll_cursor")
        cursor = datetime.fromisoformat(cursor_raw) if cursor_raw else None
        started = datetime.now(timezone.utc) - timedelta(seconds=30)  # overlap guards against clock skew
        items = parse_reservation_list(await self.ontopo.list_reservations(cursor))
        for item in items:
            self.reservations.accept_webhook({"event": "reservation.polled", "reservation": item})
        self.store.set_meta("ontopo_poll_cursor", started.isoformat())
        if items:
            self.request_inbox_processing()
        return len(items)

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        return {
            "availability": {
                "lastPushedAt": self.store.get_meta("availability_pushed_at"),
                "lastCheckedAt": self.store.get_meta("availability_checked_at"),
                "lastError": self.last_availability_error,
            },
            "events": self.store.event_counts(),
        }
