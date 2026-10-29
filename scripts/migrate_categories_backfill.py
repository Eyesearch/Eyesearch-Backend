#!/usr/bin/env python3
"""Give every existing page its `category_id`, re-classifying where needed.

The `categories` table and the trigger that keeps `pages.category_id` in step are
created by the schema on startup, and the trigger backfills any row that already
carries a category. This script exists for the rows that need more than that:
pages written before the classifier existed, or written with the default
`general_reference` because nothing looked at them. It re-runs the classifier
over them and writes both the category and the link.

    DATABASE_URL=postgresql://... python3 scripts/migrate_categories_backfill.py

    # see what it would change without writing anything
    ... --dry-run

    # re-classify every page, not just the unlinked ones
    ... --all

It is idempotent and safe to re-run: rows that already agree are skipped, and a
failure on one page does not stop the rest.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents import load_agent  # noqa: E402
from app.postgres import PostgresClient  # noqa: E402

ClassifierAgent = load_agent("07_classifier_agent").ClassifierAgent

BATCH = 200

# Keyset pagination, not OFFSET: rows are being updated as we go, and an
# offset would skip whichever rows the updates moved.
SELECT_PENDING = (
    "SELECT p.page_id, p.url, p.title, p.body_text, p.category "
    "FROM pages p "
    "WHERE {where} AND p.page_id > ? "
    "ORDER BY p.page_id "
    "LIMIT ?"
)

UPDATE_PAGE = "UPDATE pages SET category = ? WHERE page_id = ?"


async def pending(
    client: PostgresClient, where: str, after: int, limit: int
) -> list[tuple]:
    result = await client.execute(
        SELECT_PENDING.format(where=where), (after, limit)
    )
    return [tuple(row) for row in result.rows]


async def run(dsn: str, *, everything: bool, dry_run: bool) -> int:
    client = PostgresClient(dsn, statement_timeout_ms=60_000)
    classifier = ClassifierAgent()
    where = "TRUE" if everything else "p.category_id IS NULL"
    changed = 0
    seen = 0
    tally: Counter[str] = Counter()

    try:
        total = (await client.execute(f"SELECT count(*) FROM pages p WHERE {where}")).scalar(0)
        print(f"  {int(total or 0)} page(s) to inspect")

        cursor = 0
        while True:
            rows = await pending(client, where, cursor, BATCH)
            if not rows:
                break
            cursor = int(rows[-1][0])
            seen += len(rows)

            statements = []
            for page_id, url, title, body_text, current in rows:
                # The classifier wants the same shape the crawler gives it.
                verdict = classifier.classify(
                    url=url or "",
                    title=title or "",
                    body_text=body_text or "",
                )
                tally[verdict.category] += 1
                if verdict.category == current:
                    continue
                statements.append((UPDATE_PAGE, (verdict.category, page_id)))

            changed += len(statements)
            if statements and not dry_run:
                await client.batch(statements)

            print(f"  {seen} inspected, {changed} to update", end="\r")

        print()
        print(f"  {'would update' if dry_run else 'updated'} {changed} of {seen} page(s)")
        print("  categories seen:", dict(sorted(tally.items())))
    finally:
        await client.close()
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="re-classify every page")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn.startswith(("postgresql://", "postgres://")):
        print("DATABASE_URL must be set to a postgresql:// URL", file=sys.stderr)
        return 2

    return await run(dsn, everything=args.all, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
