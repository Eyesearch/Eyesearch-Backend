#!/usr/bin/env python3
"""Queue specific URLs for the crawler, or wipe the index for a fresh start.

The crawler takes work from `crawl_queue`, so this is how a fresh indexing run
is triggered without editing code or rebuilding the image: the rows go in as
`pending` and the worker picks them up on its next pass.

    # start clean, then index two sites
    python3 scripts/enqueue_urls.py --reset --yes
    python3 scripts/enqueue_urls.py https://karnataka.gov.in/ https://thehindu.com/

    # or from a file, one URL per line, # for comments
    python3 scripts/enqueue_urls.py --file seeds.txt

A URL is fetched whatever the whitelist says, because the whitelist only gates
*link discovery* — the pages a crawl is allowed to follow next. To let a seeded
site expand, its domain must be in WHITELISTED_DOMAINS, or set
ALLOW_GLOBAL_DOMAINS=true.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.postgres import PostgresClient  # noqa: E402

TABLES = ("crawl_queue", "pages", "sites")


def domain_of(url: str) -> str:
    """The registrable host a queue row is filed under, without a leading www."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def read_urls(args) -> list[str]:
    urls: list[str] = list(args.urls)
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)

    cleaned: list[str] = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            print(f"  skipping (not an http URL): {url}", file=sys.stderr)
            continue
        if not domain_of(url):
            print(f"  skipping (no host): {url}", file=sys.stderr)
            continue
        cleaned.append(url)
    return list(dict.fromkeys(cleaned))


async def reset(client: PostgresClient) -> None:
    """Empty the index and restart the identity counters."""
    for table in TABLES:
        await client.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
    print("  index cleared: crawl_queue, pages, sites (identity counters reset)")


async def enqueue(client: PostgresClient, urls: list[str]) -> int:
    statement = (
        "INSERT INTO crawl_queue (url, domain, status) VALUES (?, ?, 'pending') "
        "ON CONFLICT (url) DO NOTHING"
    )
    added = 0
    for start in range(0, len(urls), 200):
        chunk = urls[start : start + 200]
        results = await client.batch(
            [(statement, (url, domain_of(url))) for url in chunk]
        )
        added += sum(1 for result in results if result.rows_affected > 0)
    return added


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*", help="URLs to queue")
    parser.add_argument("--file", help="read URLs from a file, one per line")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="empty crawl_queue, pages and sites first (requires --yes)",
    )
    parser.add_argument("--yes", action="store_true", help="confirm --reset")
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be queued"
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn.startswith(("postgresql://", "postgres://")):
        print("DATABASE_URL must be set to a postgresql:// URL", file=sys.stderr)
        return 2

    urls = read_urls(args)

    if args.reset and not args.yes:
        print("--reset deletes every page and site. Re-run with --yes to confirm.", file=sys.stderr)
        return 2

    if args.dry_run:
        if args.reset:
            print("  would clear: " + ", ".join(TABLES))
        for url in urls:
            print(f"  would queue {url}  (domain {domain_of(url)})")
        return 0

    client = PostgresClient(dsn)
    try:
        if args.reset:
            await reset(client)

        if urls:
            added = await enqueue(client, urls)
            print(f"  queued {added} of {len(urls)} URLs ({len(urls) - added} were already queued)")

        pending = (await client.execute(
            "SELECT count(*) FROM crawl_queue WHERE status = 'pending'"
        )).scalar(0)
        pages = (await client.execute("SELECT count(*) FROM pages")).scalar(0)
        print(f"  queue now holds {pending} pending, index holds {pages} pages")
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
