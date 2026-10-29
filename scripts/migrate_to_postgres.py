#!/usr/bin/env python3
"""Create the PostgreSQL schema and optionally copy data from the old database.

Run this against the target PostgreSQL before pointing the app at it:

    DATABASE_URL=postgresql://user:pass@host:5432/eyesearch \\
        python3 scripts/migrate_to_postgres.py

To carry the existing index across as well, give it the SQLite file the
previous deployment was using:

    DATABASE_URL=postgresql://... \\
        python3 scripts/migrate_to_postgres.py --from-sqlite ./local.db

The copy is idempotent: rows are inserted with ON CONFLICT DO NOTHING, so a
half-finished run can simply be repeated. It is also read-only on the source —
the SQLite file is opened with `mode=ro` and never modified.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.postgres import PostgresClient  # noqa: E402
from app.schema_postgres import POSTGRES_SCHEMA_STATEMENTS  # noqa: E402

#: Copied in dependency order: sites before pages, because pages reference them.
TABLES = ("sites", "pages", "crawl_queue")

BATCH = 500


async def apply_schema(client: PostgresClient) -> None:
    print(f"applying {len(POSTGRES_SCHEMA_STATEMENTS)} schema statements")
    await client.batch([(sql, ()) for sql in POSTGRES_SCHEMA_STATEMENTS])
    print("schema ready")


def read_sqlite_rows(path: str, table: str) -> tuple[list[str], list[tuple]]:
    """Rows and column names from the old database, opened read-only."""
    uri = f"file:{path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        cursor = connection.execute(f"SELECT * FROM {table}")
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
        return columns, rows
    finally:
        connection.close()


async def copy_table(client: PostgresClient, path: str, table: str) -> int:
    columns, rows = read_sqlite_rows(path, table)
    if not rows:
        print(f"  {table}: source is empty")
        return 0

    # `rowid` is SQLite's implicit queue key; PostgreSQL calls it `id`.
    target_columns = ["id" if column == "rowid" else column for column in columns]
    placeholders = ", ".join("?" for _ in target_columns)
    statement = (
        f"INSERT INTO {table} ({', '.join(target_columns)}) VALUES ({placeholders}) "
        "ON CONFLICT DO NOTHING"
    )

    copied = 0
    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        results = await client.batch([(statement, tuple(row)) for row in chunk])
        copied += sum(1 for result in results if result.rows_affected > 0)

    print(f"  {table}: {copied} of {len(rows)} rows copied")
    return copied


async def resequence(client: PostgresClient) -> None:
    """Move each identity sequence past the rows that were just inserted.

    Without this the first insert after a copy collides with a copied id, which
    looks like a duplicate-key bug rather than a migration one.
    """
    for table, column in (("sites", "site_id"), ("pages", "page_id"), ("crawl_queue", "id")):
        await client.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), "
            f"COALESCE((SELECT MAX({column}) FROM {table}), 1), "
            f"(SELECT MAX({column}) IS NOT NULL FROM {table}))"
        )
    print("  identity sequences resequenced")


async def report(client: PostgresClient) -> None:
    for table in TABLES:
        count = (await client.execute(f"SELECT count(*) FROM {table}")).scalar(0)
        print(f"  {table}: {count} rows")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-sqlite",
        metavar="PATH",
        help="copy rows from this SQLite file after creating the schema",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="create tables and indexes, copy nothing",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    if not (dsn.startswith("postgresql://") or dsn.startswith("postgres://")):
        print("DATABASE_URL must be a postgresql:// URL", file=sys.stderr)
        return 2

    client = PostgresClient(dsn)
    try:
        await apply_schema(client)

        if args.from_sqlite and not args.schema_only:
            source = args.from_sqlite
            if not Path(source).exists():
                print(f"source database not found: {source}", file=sys.stderr)
                return 2
            print(f"copying from {source}")
            for table in TABLES:
                await copy_table(client, source, table)
            await resequence(client)

        print("target now holds:")
        await report(client)
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
