#!/usr/bin/env python3
"""Park every queued URL belonging to a domain that has stopped answering.

Run this once against production to clear the backlog the dead hosts built up.
The circuit breaker stops it recurring, but it only knows about domains it has
watched fail in *this* run — a queue already full of a dead host's URLs needs
clearing once.

    python scripts/park_dead_domains.py --dry-run
    python scripts/park_dead_domains.py --domain bemlindia.in --domain iudx.org.in

`--dry-run` is the default when no domain is given: this writes to the queue,
and guessing at which hosts are dead is not a thing to do by accident.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.config import get_settings
from app.libsql_http import build_client

#: Domains observed failing through the outage. Not authoritative — the
#: breaker is — but these are the ones with hundreds of queued rows each.
KNOWN_DEAD = (
    "bemlindia.in",
    "iudx.org.in",
    "iitdh.ac.in",
    "pawanhans.co.in",
    "indiacode.gov.in",
)


async def park(client, domains, *, dry_run: bool) -> int:
    placeholders = ",".join("?" for _ in domains)
    where = (
        f"status = 'pending' AND domain IN ({placeholders})"
    )
    counted = await client.execute(f"SELECT COUNT(*) FROM crawl_queue WHERE {where}", tuple(domains))
    total = int(counted.scalar(0) or 0)
    if dry_run or not total:
        return total

    await client.execute(
        "UPDATE crawl_queue SET status = 'skipped', "
        "last_error = 'circuit_breaker_dead_domain' "
        f"WHERE {where}",
        tuple(domains),
    )
    return total


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true", help="write the change")
    args = parser.parse_args()

    domains = tuple(args.domain or KNOWN_DEAD)
    dry_run = args.dry_run or not args.apply

    settings = get_settings()
    client = build_client(settings)
    try:
        total = await park(client, domains, dry_run=dry_run)
    finally:
        await client.close()

    if dry_run:
        print(f"DRY RUN: {total} pending rows would be parked across {len(domains)} domains")
        print("re-run with --apply to write it")
    else:
        print(f"parked {total} pending rows across {len(domains)} domains")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
