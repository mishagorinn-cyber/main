"""Syrve table orders -> Ontopo.

The Ontopo POS API has no separate "free tables" feed: Ontopo learns which
tables are taken from the orders the POS reports through /pos/setReservation.

* An order opened on the table of an Ontopo reservation (around its time)
  marks that reservation "seated" and carries the order; when the bill is
  paid the reservation becomes "done" and Ontopo frees the table.
* Any other order (a walk-in) is created in Ontopo as a POS-origin
  reservation: a new syncId, the order, and an empty reservation object.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from .config import Settings
from .ontopo.mapping import (
    STAGE_CHECK,
    STAGE_ORDERED,
    STAGE_PAID,
    STATUS_CANCELED,
    STATUS_DONE,
    STATUS_SEATED,
    build_sync_data,
    iso,
)
from .storage import Link, Store
from .syrve.client import parse_syrve_time
from .tables import TableDirectory

log = logging.getLogger(__name__)

SYRVE_OPEN = {"New", "Bill"}
SYRVE_CLOSED = "Closed"
SYRVE_DELETED = "Deleted"
WALK_IN_PREFIX = "syrve-"


def _digest(body: dict) -> str:
    stable = {k: v for k, v in body.items() if k != "syncAt"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def _number(value) -> float | int | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else round(f, 2)


class OrderSync:
    def __init__(self, settings: Settings, syrve, ontopo, store: Store, directory: TableDirectory):
        self.settings = settings
        self.syrve = syrve
        self.ontopo = ontopo
        self.store = store
        self.directory = directory

    def now(self) -> datetime:
        return datetime.now(self.settings.tz).replace(tzinfo=None, microsecond=0)

    async def run(self) -> int:
        """One pass. Returns how many updates were sent to Ontopo."""
        tables = await self.directory.tables()
        known = {t.id for t in tables}
        if not known:
            return 0
        orders = await self.syrve.table_orders(
            sorted(known), self.now() - timedelta(hours=24), ["New", "Bill", SYRVE_CLOSED, SYRVE_DELETED]
        )
        sent = 0
        for order in orders:
            table_ids = [t for t in order.get("tableIds") or [] if t in known]
            if table_ids and order.get("id") and await self._sync_order(order, table_ids):
                sent += 1
        return sent

    async def _sync_order(self, order: dict, table_ids: list[str]) -> bool:
        order_id = str(order["id"])
        status = order.get("status") or "New"
        link = self.store.link_for_order(order_id)

        if link is None:
            if status not in SYRVE_OPEN:
                return False  # finished before we ever saw it: nothing to tell Ontopo
            link = self._attach(order_id, order, table_ids)
            if link is None:
                return False
        if link.state != "active":
            return False

        if link.origin == "ontopo" and status == SYRVE_DELETED:
            # Order removed on the POS: the reservation is no longer seated with it.
            self.store.update_link(link.sync_id, syrve_order_id=None, order_digest=None)
            return False

        body = self._build(link, order, table_ids, status)
        digest = _digest(body)
        if digest == link.order_digest:
            return False

        response = await self.ontopo.set_reservation(body)
        changes: dict = {"order_digest": digest, "table_ids": table_ids}
        returned = (response or {}).get("reservation") if isinstance(response, dict) else None
        if isinstance(returned, dict) and returned:
            changes["ontopo_reservation"] = returned
            if returned.get("reservationId"):
                changes["ontopo_reservation_id"] = str(returned["reservationId"])
        elif link.origin == "ontopo":
            changes["ontopo_reservation"] = body["reservation"]
        if status == SYRVE_CLOSED:
            changes["state"] = "done"
        elif status == SYRVE_DELETED:
            changes["state"] = "cancelled"
        self.store.update_link(link.sync_id, **changes)
        log.info("Sent order %s (%s) to Ontopo as %s", order_id, status, link.sync_id)
        return True

    def _attach(self, order_id: str, order: dict, table_ids: list[str]) -> Link | None:
        opened = self._opened_at(order)
        reservation = self._matching_reservation(table_ids, opened)
        if reservation is not None:
            log.info("Order %s belongs to Ontopo reservation %s", order_id, reservation.sync_id)
            return self.store.update_link(reservation.sync_id, syrve_order_id=order_id)
        if not self.settings.orders.create_walk_ins:
            return None
        return self.store.update_link(
            WALK_IN_PREFIX + order_id,
            origin="pos",
            state="active",
            syrve_order_id=order_id,
            start=opened,
            table_ids=table_ids,
            party_size=self._guests(order),
        )

    def _matching_reservation(self, table_ids: list[str], opened: datetime) -> Link | None:
        early = timedelta(minutes=self.settings.orders.early_arrival_minutes)
        default = self.settings.reservations.default_duration_minutes
        best: Link | None = None
        for link in self.store.links_starting_between(opened - timedelta(days=1), opened + early):
            if link.state != "active" or link.syrve_order_id or not link.start:
                continue
            if not set(link.table_ids) & set(table_ids):
                continue
            end = link.start + timedelta(minutes=link.duration_minutes or default)
            if not (link.start - early <= opened <= end):
                continue
            if best is None or abs(link.start - opened) < abs(best.start - opened):
                best = link
        return best

    def _opened_at(self, order: dict) -> datetime:
        raw = order.get("whenCreated") or order.get("whenOpened")
        if raw:
            try:
                return parse_syrve_time(str(raw))
            except ValueError:
                pass
        return self.now()

    @staticmethod
    def _guests(order: dict) -> int | None:
        guests = order.get("guestsInfo") or {}
        count = guests.get("count") if isinstance(guests, dict) else None
        return _number(count if count is not None else order.get("guestsCount"))

    def _build(self, link: Link, order: dict, table_ids: list[str], status: str) -> dict:
        tz = self.settings.tz
        if link.origin == "ontopo" or link.order_digest is not None:
            if status == SYRVE_CLOSED:
                new_status = STATUS_DONE
            elif status == SYRVE_DELETED:
                new_status = STATUS_CANCELED
            else:
                new_status = STATUS_SEATED
            reservation = {**(link.ontopo_reservation or {}), "status": new_status}
        else:
            reservation = {}  # spec: POS-origin create sends an empty reservation
        return build_sync_data(
            link.sync_id, datetime.now(timezone.utc), tz, reservation, self._order(order, table_ids, status)
        )

    def _order(self, order: dict, table_ids: list[str], status: str) -> dict:
        tz = self.settings.tz
        by_id = self.directory.by_id()
        items = []
        for item in order.get("items") or []:
            if item.get("deleted") or item.get("status") == "Deleted":
                continue
            product = item.get("product") or {}
            items.append(
                {
                    "itemId": str(product.get("id") or item.get("productId") or ""),
                    "itemName": product.get("name") or item.get("name") or "",
                    "quantity": _number(item.get("amount")),
                    "price": _number(item.get("price")),
                }
            )
        if status == SYRVE_CLOSED:
            stage = STAGE_PAID
        elif status == "Bill":
            stage = STAGE_CHECK
        else:
            stage = STAGE_ORDERED if items else None
        discount = sum(float(d.get("sum") or 0) for d in order.get("discounts") or [])
        body = {
            "posOrderId": str(order["id"]),
            "orderNumber": _number(order.get("number")),
            "openedAt": iso(self._opened_at(order), tz),
            "netTotal": _number(order.get("sum")),
            "discountAmount": _number(discount),
            "dinnersCount": self._guests(order),
            "stage": stage,
            "tables": [by_id[t].number for t in table_ids if t in by_id and by_id[t].number is not None],
            "items": items if self.settings.orders.include_items else [],
        }
        return {k: v for k, v in body.items() if v is not None}
