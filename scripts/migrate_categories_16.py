"""Move an existing index from the twelve category codes to the sixteen.

The taxonomy changed: FINANCE became FIN, COMMERCE became COMM, AGRI became
FOOD, PAY merged into FIN, and TRANS, UTIL, SOCIAL, CORP and MEDIA are new.

Pages point at a category by id, so the renames are done in place — the row keeps
its id and only its code changes, which means no page has to be touched. PAY is
the one that cannot be renamed: its pages have to be re-pointed at FIN before the
row can go.

Run it with --dry-run first. It prints what it would do and changes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.postgres import PostgresClient  # noqa: E402
from app.schema_postgres import CATEGORY_SEED  # noqa: E402

#: Old code -> new code, where the row can simply be renamed.
RENAMES = {"FINANCE": "FIN", "COMMERCE": "COMM", "AGRI": "FOOD"}
#: Old code -> the code its pages now belong to. The row is removed after.
MERGES = {"PAY": "FIN"}


async def migrate(client: PostgresClient, *, dry_run: bool) -> dict[str, int]:
    counts: dict[str, int] = {}

    for new_code, name in CATEGORY_SEED:
        result = await client.execute(
            "INSERT INTO categories (code, name) VALUES (?, ?) "
            "ON CONFLICT (code) DO NOTHING",
            (new_code, name),
        )
        counts[f"seeded:{new_code}"] = result.rows_affected

    for old, new in RENAMES.items():
        result = await client.execute(
            "UPDATE categories SET code = ? WHERE code = ?", (new, old)
        )
        counts[f"renamed:{old}->{new}"] = result.rows_affected

    for old, new in MERGES.items():
        moved = await client.execute(
            "UPDATE pages SET category_id = (SELECT id FROM categories WHERE code = ?) "
            "WHERE category_id = (SELECT id FROM categories WHERE code = ?)",
            (new, old),
        )
        counts[f"moved:{old}->{new}"] = moved.rows_affected
        removed = await client.execute("DELETE FROM categories WHERE code = ?", (old,))
        counts[f"removed:{old}"] = removed.rows_affected

    stray = await client.execute(
        "SELECT COUNT(*) FROM pages p WHERE p.category_id IS NOT NULL "
        "AND p.category_id NOT IN (SELECT id FROM categories)"
    )
    counts["orphaned_pages"] = int(stray.scalar(0) or 0)

    if dry_run:
        await client.execute("ROLLBACK")

    return counts


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")

    client = PostgresClient(dsn, statement_timeout_ms=60_000)
    try:
        counts = await migrate(client, dry_run=args.dry_run)
        for key, value in sorted(counts.items()):
            print(f"  {key:32} {value}")
        print("  dry run — nothing written" if args.dry_run else "  migration applied")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
