"""Ontopo -> Syrve reservation sync.

Every SyncData record (webhook or poll) is stored in the inbox first and then
reconciled: the Ontopo reservation is the desired state, the Syrve reserve is
made to match it. Reconciliation is idempotent, so duplicates, echoes of our
own updates and replays are harmless.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from .config import Settings
from .models import InboundReservation, ReservationState, Table
from .ontopo.client import OntopoError
from .ontopo.mapping import PayloadError, build_sync_data, parse_sync_data, parse_sync_id
from .storage import Event, Store
from .syrve.client import SyrveError, SyrveReserveFailed
from .tables import TableDirectory, is_table_free, pick_tables

log = logging.getLogger(__name__)

STATE_ACTIVE = "active"
STATE_CANCELLED = "cancelled"
STATE_DONE = "done"
STATE_FAILED = "failed"


class NoTableAvailable(Exception):
    pass


class ReservationSync:
    def __init__(self, settings: Settings, syrve, ontopo, store: Store, directory: TableDirectory):
        self.settings = settings
        self.syrve = syrve
        self.ontopo = ontopo
        self.store = store
        self.directory = directory

    # ------------------------------------------------------------------ inbox

    def accept(self, payload: dict, source: str = "webhook") -> int:
        """Store a SyncData record for processing. Returns the inbox id."""
        return self.store.enqueue(parse_sync_id(payload), source, payload)

    async def process_due(self) -> int:
        processed = 0
        for event in self.store.due_events():
            await self._process_event(event)
            processed += 1
        return processed

    async def _process_event(self, event: Event) -> None:
        cfg = self.settings.reservations
        try:
            reservation = parse_sync_data(event.payload)
        except PayloadError as exc:
            log.error("Dropping malformed SyncData %s (event %s): %s", event.sync_id, event.id, exc)
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
            log.warning("Event %s for syncId %s failed (attempt %d): %s", event.id, event.sync_id, attempts, exc)
            self.store.mark_retry(event.id, str(exc), delay, give_up)
            if give_up:
                link = self.store.get_link(reservation.sync_id)
                if link and link.state == STATE_ACTIVE and link.syrve_reserve_id:
                    # Keep the existing Syrve reserve linked so a later cancel still reaches it.
                    self.store.update_link(reservation.sync_id, error=f"update not applied: {exc}")
                else:
                    self.store.update_link(reservation.sync_id, origin="ontopo", state=STATE_FAILED, error=str(exc))

    # ------------------------------------------------------------------ reconcile

    async def apply(self, res: InboundReservation) -> str:
        """Make Syrve match the Ontopo reservation. Returns a short outcome label."""
        link = self.store.get_link(res.sync_id)
        seen = {"ontopo_reservation": res.raw, "ontopo_reservation_id": res.reservation_id or None}

        if link and link.origin == "pos":
            # Our own walk-in order echoed back by Ontopo: Syrve already has it.
            self.store.update_link(res.sync_id, **seen)
            return "noop"

        if res.state is ReservationState.CANCELLED:
            if link and link.state == STATE_ACTIVE and link.syrve_reserve_id:
                await self.syrve.cancel_reserve(link.syrve_reserve_id, _cancel_reason(res.raw_status))
                self.store.update_link(res.sync_id, state=STATE_CANCELLED, error=None, **seen)
                log.info("Cancelled Syrve reserve %s (Ontopo %s, %s)", link.syrve_reserve_id, res.sync_id, res.raw_status)
                return "cancelled"
            if not link or link.state != STATE_CANCELLED:
                self.store.update_link(res.sync_id, origin="ontopo", state=STATE_CANCELLED, start=res.start,
                                       party_size=res.party_size, **seen)
            return "noop"

        if res.state is ReservationState.DONE:
            # The visit is over; the Syrve reserve stays as history.
            self.store.update_link(res.sync_id, origin="ontopo", state=STATE_DONE, **seen)
            return "noop"

        fingerprint = res.fingerprint()
        if link and link.state == STATE_ACTIVE and link.fingerprint == fingerprint:
            self.store.update_link(res.sync_id, **seen)
            return "noop"

        previous_id = link.syrve_reserve_id if link and link.state == STATE_ACTIVE else None
        duration = res.duration_minutes or self.settings.reservations.default_duration_minutes
        try:
            tables = await self._choose_tables(res, duration, previous_id, link.table_ids if previous_id else [])
        except NoTableAvailable as exc:
            return self._not_placed(res, str(exc), keep_previous=previous_id is not None, seen=seen)

        reserve_id = str(uuid.uuid4())
        await self.syrve.create_reserve(
            reserve_id=reserve_id,
            external_number=res.reservation_id or res.sync_id,
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
            return self._not_placed(res, str(exc), keep_previous=previous_id is not None, seen=seen)

        # The new reserve is in place; now drop the old one (create-then-cancel keeps the guest covered).
        if previous_id:
            try:
                await self.syrve.cancel_reserve(previous_id, "Other")
            except SyrveError:
                log.exception("Could not cancel superseded Syrve reserve %s", previous_id)

        labels = [self.directory.label_for(t) for t in tables]
        if self.settings.reservations.report_assigned_tables and sorted(labels) != sorted(res.table_labels):
            if await self._report_tables(res, labels):
                # Ontopo will echo the new tables back; that must not look like a change.
                fingerprint = res.fingerprint(labels)
                seen["ontopo_reservation"] = {**res.raw, "table": labels}

        self.store.update_link(
            res.sync_id,
            origin="ontopo",
            state=STATE_ACTIVE,
            syrve_reserve_id=reserve_id,
            fingerprint=fingerprint,
            start=res.start,
            duration_minutes=duration,
            party_size=res.party_size,
            table_ids=[t.id for t in tables],
            error=None,
            **seen,
        )
        log.info(
            "%s Syrve reserve %s for Ontopo %s on table(s) %s",
            "Moved" if previous_id else "Created",
            reserve_id,
            res.sync_id,
            ", ".join(labels),
        )
        return "updated" if previous_id else "created"

    def _not_placed(self, res: InboundReservation, reason: str, keep_previous: bool, seen: dict) -> str:
        log.warning("Ontopo reservation %s not placed in Syrve: %s", res.sync_id, reason)
        if keep_previous:
            # The change could not be applied; the existing Syrve reserve stays as it was.
            self.store.update_link(res.sync_id, error=f"update not applied: {reason}", **seen)
        else:
            self.store.update_link(
                res.sync_id, origin="ontopo", state=STATE_FAILED, start=res.start, party_size=res.party_size,
                error=reason, **seen,
            )
        return "rejected"

    async def _report_tables(self, res: InboundReservation, labels: list[str]) -> bool:
        reservation = {**res.raw, "table": labels}
        body = build_sync_data(res.sync_id, datetime.now(timezone.utc), self.settings.tz, reservation)
        try:
            await self.ontopo.set_reservation(body)
            return True
        except (OntopoError, httpx.HTTPError):
            log.exception("Could not send assigned tables of %s to Ontopo", res.sync_id)
            return False

    async def _choose_tables(
        self, res: InboundReservation, duration: int, previous_reserve_id: str | None, previous_tables: list[str]
    ) -> list[Table]:
        cfg = self.settings.reservations
        tables = await self.directory.tables()
        by_id = {t.id: t for t in tables}
        ignore = frozenset({previous_reserve_id}) if previous_reserve_id else frozenset()
        buffer = timedelta(minutes=cfg.buffer_minutes)
        end = res.start + timedelta(minutes=duration)

        section_ids = sorted({t.section_id for t in tables})
        busy = await self.syrve.workload(section_ids, res.start - timedelta(days=1), end + timedelta(days=1))

        def all_free(candidates: list[Table]) -> bool:
            return all(is_table_free(t.id, res.start, end, busy, buffer, ignore) for t in candidates)

        requested = self._resolve_labels(res.table_labels)
        if requested:
            if all_free(requested) or not cfg.auto_assign_tables:
                return requested
            log.warning("Tables %s requested by Ontopo for %s are busy in Syrve; auto-assigning",
                        res.table_labels, res.sync_id)

        previous = [by_id[t] for t in previous_tables if t in by_id]
        if previous and sum(t.capacity for t in previous) >= res.party_size and all_free(previous):
            return previous

        if not cfg.auto_assign_tables:
            raise NoTableAvailable("Ontopo gave no usable table and auto-assignment is disabled")
        chosen = pick_tables(
            tables, busy, res.start, duration, res.party_size, cfg.buffer_minutes, cfg.allow_table_combining, ignore
        )
        if not chosen:
            raise NoTableAvailable(f"no free table for {res.party_size} guests at {res.start:%Y-%m-%d %H:%M}")
        return chosen

    def _resolve_labels(self, labels: list[str]) -> list[Table]:
        resolved: list[Table] = []
        for label in labels:
            table = self.directory.resolve_label(label)
            if table is None:
                log.warning("Ontopo table %r has no Syrve match (add it to reservations.table_map)", label)
                return []
            resolved.append(table)
        return resolved


def _cancel_reason(raw_status: str) -> str:
    status = raw_status.lower()
    if status == "noshow":
        return "ClientNotAppeared"
    if status in {"canceled", "cancelled"}:
        return "ClientRefused"
    return "Other"


def _comment(res: InboundReservation) -> str:
    parts = [f"Ontopo #{res.reservation_id or res.sync_id}"]
    if res.seating_requests:
        parts.append("Requests: " + ", ".join(res.seating_requests))
    return " | ".join(parts)
