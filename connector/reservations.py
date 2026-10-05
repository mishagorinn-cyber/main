"""Ontopo -> Syrve reservation sync.

Every webhook (or polled reservation) is stored in the inbox first and then
reconciled: the Ontopo reservation is the desired state, the Syrve reserve is
made to match it. Reconciliation is idempotent, so duplicates and replays are
harmless.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Awaitable, Callable

import httpx

from .availability import is_table_free, pick_tables
from .config import Settings
from .models import InboundReservation, ReservationState, Table
from .ontopo.client import OntopoError
from .ontopo.mapping import PayloadError, build_status_payload, parse_reservation, unwrap_event
from .storage import Event, Store
from .syrve.client import SyrveError, SyrveReserveFailed

log = logging.getLogger(__name__)

STATE_ACTIVE = "active"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"


class NoTableAvailable(Exception):
    pass


class ReservationSync:
    def __init__(
        self,
        settings: Settings,
        syrve,
        ontopo,
        store: Store,
        tables_loader: Callable[[], Awaitable[list[Table]]],
        on_change: Callable[[], None] | None = None,
    ):
        self.settings = settings
        self.syrve = syrve
        self.ontopo = ontopo
        self.store = store
        self.tables_loader = tables_loader
        self.on_change = on_change or (lambda: None)

    # ------------------------------------------------------------------ inbox

    def accept_webhook(self, payload: dict) -> int:
        """Validate minimally and store the event. Returns the inbox id."""
        event, reservation = unwrap_event(payload)
        res_id = reservation.get("id") or reservation.get("reservationId") or reservation.get("reservation_id")
        if not res_id:
            raise PayloadError("reservation id is missing")
        return self.store.enqueue(str(res_id), event, payload)

    async def process_due(self) -> int:
        processed = 0
        for event in self.store.due_events():
            await self._process_event(event)
            processed += 1
        return processed

    async def _process_event(self, event: Event) -> None:
        cfg = self.settings.reservations
        try:
            event_type, data = unwrap_event(event.payload)
            reservation = parse_reservation(data, self.settings, event_type)
        except PayloadError as exc:
            log.error("Dropping malformed event %s: %s", event.id, exc)
            self.store.mark_retry(event.id, f"malformed payload: {exc}", 0, give_up=True)
            return
        try:
            await self.apply(reservation)
            self.store.mark_done(event.id)
        except Exception as exc:  # network, API and unexpected errors are all retried with backoff
            if not isinstance(exc, (SyrveError, OntopoError, httpx.HTTPError)):
                log.exception("Unexpected error while processing event %s", event.id)
            attempts = event.attempts + 1
            give_up = attempts >= cfg.max_attempts
            delay = cfg.retry_base_seconds * 2 ** (attempts - 1)
            log.warning("Event %s for reservation %s failed (attempt %d): %s", event.id, reservation.id, attempts, exc)
            self.store.mark_retry(event.id, str(exc), delay, give_up)
            if give_up:
                link = self.store.get_link(reservation.id)
                if link and link.state == STATE_ACTIVE:
                    # Keep the existing Syrve reserve linked so a later cancel still reaches it.
                    self.store.save_link(
                        reservation.id,
                        state=STATE_ACTIVE,
                        syrve_reserve_id=link.syrve_reserve_id,
                        fingerprint=link.fingerprint,
                        start=datetime.fromisoformat(link.start) if link.start else None,
                        party_size=link.party_size,
                        table_ids=link.table_ids,
                        error=f"update not applied: {exc}",
                    )
                else:
                    self.store.save_link(reservation.id, state=STATE_FAILED, error=str(exc))
                await self._report(reservation.id, "rejected", message=f"POS sync failed: {exc}")

    # ------------------------------------------------------------------ reconcile

    async def apply(self, res: InboundReservation) -> str:
        """Make Syrve match the Ontopo reservation. Returns a short outcome label."""
        link = self.store.get_link(res.id)

        if res.state is ReservationState.CANCELLED:
            if link and link.state == STATE_ACTIVE and link.syrve_reserve_id:
                await self.syrve.cancel_reserve(link.syrve_reserve_id, _cancel_reason(res.raw_status))
                self.store.save_link(
                    res.id,
                    state=STATE_CANCELLED,
                    syrve_reserve_id=link.syrve_reserve_id,
                    fingerprint=link.fingerprint,
                    start=res.start,
                    party_size=res.party_size,
                    table_ids=link.table_ids,
                )
                self.on_change()
                log.info("Cancelled Syrve reserve %s for Ontopo %s", link.syrve_reserve_id, res.id)
                return "cancelled"
            if not link:
                self.store.save_link(res.id, state=STATE_CANCELLED, start=res.start, party_size=res.party_size)
            return "noop"

        fingerprint = res.fingerprint()
        if link and link.state == STATE_ACTIVE and link.fingerprint == fingerprint:
            return "noop"

        previous_id = link.syrve_reserve_id if link and link.state == STATE_ACTIVE else None
        try:
            tables = await self._choose_tables(res, previous_id, link.table_ids if previous_id else [])
        except NoTableAvailable as exc:
            return await self._reject(res, str(exc), keep_previous=previous_id is not None)

        reserve_id = str(uuid.uuid4())
        duration = res.duration_minutes or self.settings.reservations.default_duration_minutes
        await self.syrve.create_reserve(
            reserve_id=reserve_id,
            external_number=f"ontopo-{res.id}",
            start=res.start,
            duration_minutes=duration,
            table_ids=[t.id for t in tables],
            guests_count=res.party_size,
            name=res.guest.first_name or res.guest.full_name,
            surname=res.guest.last_name,
            phone=res.guest.phone,
            email=res.guest.email,
            comment=_comment(res),
        )
        try:
            await self.syrve.wait_reserve_created(reserve_id)
        except SyrveReserveFailed as exc:
            return await self._reject(res, str(exc), keep_previous=previous_id is not None)

        # New reserve is in place; now drop the old one (create-then-cancel keeps the guest covered).
        if previous_id:
            try:
                await self.syrve.cancel_reserve(previous_id, "Other")
            except SyrveError:
                log.exception("Could not cancel superseded Syrve reserve %s", previous_id)

        self.store.save_link(
            res.id,
            state=STATE_ACTIVE,
            syrve_reserve_id=reserve_id,
            fingerprint=fingerprint,
            start=res.start,
            party_size=res.party_size,
            table_ids=[t.id for t in tables],
        )
        self.on_change()
        await self._report(res.id, "confirmed", reserve_id, tables)
        log.info(
            "%s Syrve reserve %s for Ontopo %s on tables %s",
            "Moved" if previous_id else "Created",
            reserve_id,
            res.id,
            ", ".join(t.name for t in tables),
        )
        return "updated" if previous_id else "created"

    async def _reject(self, res: InboundReservation, reason: str, keep_previous: bool) -> str:
        log.warning("Ontopo reservation %s not placed in Syrve: %s", res.id, reason)
        if keep_previous:
            # The change could not be applied; the existing Syrve reserve stays as it was.
            link = self.store.get_link(res.id)
            self.store.save_link(
                res.id,
                state=STATE_ACTIVE,
                syrve_reserve_id=link.syrve_reserve_id,
                fingerprint=link.fingerprint,
                start=datetime.fromisoformat(link.start) if link.start else None,
                party_size=link.party_size,
                table_ids=link.table_ids,
                error=f"update not applied: {reason}",
            )
        else:
            self.store.save_link(
                res.id, state=STATE_FAILED, start=res.start, party_size=res.party_size, error=reason
            )
        await self._report(res.id, "rejected", message=reason)
        return "rejected"

    async def _choose_tables(
        self, res: InboundReservation, previous_reserve_id: str | None, previous_tables: list[str]
    ) -> list[Table]:
        cfg = self.settings.reservations
        buffer = self.settings.availability.buffer_minutes
        duration = res.duration_minutes or cfg.default_duration_minutes
        tables = await self.tables_loader()
        by_id = {t.id: t for t in tables}
        ignore = frozenset({previous_reserve_id}) if previous_reserve_id else frozenset()

        section_ids = sorted({t.section_id for t in tables})
        busy = await self.syrve.workload(
            section_ids, res.start - timedelta(days=1), res.start + timedelta(minutes=duration) + timedelta(days=1)
        )
        end = res.start + timedelta(minutes=duration)

        def all_free(candidates: list[Table]) -> bool:
            return all(is_table_free(t.id, res.start, end, busy, timedelta(minutes=buffer), ignore) for t in candidates)

        requested = self._resolve_requested(res.table_ids, tables)
        if requested:
            if all_free(requested) or not cfg.auto_assign_tables:
                return requested
            log.warning("Tables requested by Ontopo for %s are busy in Syrve; auto-assigning", res.id)

        previous = [by_id[t] for t in previous_tables if t in by_id]
        if previous and sum(t.capacity for t in previous) >= res.party_size and all_free(previous):
            return previous

        if not cfg.auto_assign_tables:
            raise NoTableAvailable("no table specified by Ontopo and auto-assignment is disabled")
        chosen = pick_tables(
            tables, busy, res.start, duration, res.party_size, buffer, cfg.allow_table_combining, ignore
        )
        if not chosen:
            raise NoTableAvailable(
                f"no free table for {res.party_size} guests at {res.start:%Y-%m-%d %H:%M}"
            )
        return chosen

    def _resolve_requested(self, ontopo_table_ids: list[str], tables: list[Table]) -> list[Table]:
        if not ontopo_table_ids:
            return []
        table_map = self.settings.reservations.table_map
        by_id = {t.id: t for t in tables}
        resolved: list[Table] = []
        for oid in ontopo_table_ids:
            target = by_id.get(table_map.get(oid, oid))
            if target is None:
                target = next((t for t in tables if str(t.number) == oid or t.name == oid), None)
            if target is None:
                log.warning("Ontopo table %s has no Syrve match (add it to reservations.table_map)", oid)
                return []
            resolved.append(target)
        return resolved

    async def _report(
        self, reservation_id: str, status: str, reserve_id: str = "", tables: list[Table] | None = None, message: str = ""
    ) -> None:
        try:
            await self.ontopo.report_status(
                reservation_id, build_status_payload(reservation_id, status, reserve_id, tables, message)
            )
        except (OntopoError, httpx.HTTPError):
            log.exception("Could not report status %s of reservation %s to Ontopo", status, reservation_id)


def _cancel_reason(raw_status: str) -> str:
    if raw_status in {"no_show", "noshow"}:
        return "ClientNotAppeared"
    if raw_status in {"cancelled", "canceled"}:
        return "ClientRefused"
    return "Other"


def _comment(res: InboundReservation) -> str:
    parts = [f"Ontopo #{res.id}"]
    if res.notes:
        parts.append(res.notes)
    return " | ".join(parts)
