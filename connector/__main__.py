"""Command line: serve | discover | preview-availability | sync-availability."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from .config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="syrve-ontopo", description="Syrve ↔ Ontopo connector")
    parser.add_argument("-c", "--config", help="path to config.yaml (default: $CONNECTOR_CONFIG or ./config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the webhook server and background sync")
    sub.add_parser("discover", help="list Syrve organizations, terminal groups, sections and tables")
    sub.add_parser("preview-availability", help="print the availability payload without sending it")
    sub.add_parser("sync-availability", help="push availability to Ontopo once")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings(args.config)

    if args.command == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(create_app(settings), host=settings.server.host, port=settings.server.port)
    elif args.command == "discover":
        asyncio.run(_discover(settings))
    elif args.command == "preview-availability":
        asyncio.run(_preview(settings))
    elif args.command == "sync-availability":
        asyncio.run(_sync(settings))


async def _discover(settings) -> None:
    from .syrve.client import SyrveClient

    syrve = SyrveClient(settings.syrve)
    try:
        orgs = await syrve.organizations()
        print("Organizations:")
        for org in orgs:
            print(f"  {org.get('id')}  {org.get('name')}")
        org_ids = [o["id"] for o in orgs] or [settings.syrve.organization_id]
        groups = await syrve.terminal_groups(org_ids)
        print("\nTerminal groups with reservations enabled:")
        for g in groups:
            print(f"  {g.get('id')}  {g.get('name')}  (organization {g.get('organizationId')})")
        if not groups:
            return
        sections = await syrve.restaurant_sections([g["id"] for g in groups])
        print("\nRestaurant sections and tables:")
        for s in sections:
            print(f"  [{s.get('id')}] {s.get('name')}  (terminal group {s.get('terminalGroupId')})")
            for t in s.get("tables", []):
                if t.get("isDeleted"):
                    continue
                print(f"      table {t.get('number')!s:>4}  seats {t.get('seatingCapacity')!s:>2}  {t.get('id')}  {t.get('name')}")
    finally:
        await syrve.aclose()


async def _preview(settings) -> None:
    from .service import Connector

    conn = Connector(settings)
    try:
        payload = await conn.availability.build_payload(refresh_tables=True)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        await conn.stop()


async def _sync(settings) -> None:
    from .service import Connector

    conn = Connector(settings)
    try:
        pushed = await conn.availability.run(force=True)
        print("pushed" if pushed else "unchanged")
    finally:
        await conn.stop()


if __name__ == "__main__":
    main()
