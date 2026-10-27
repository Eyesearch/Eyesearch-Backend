"""Turso (libSQL) schema bootstrap.

Every table is `CREATE ... IF NOT EXISTS`, so a cold start against an empty
database builds the whole thing with no manual step, and a warm start is a
no-op.

`page_fts` is **external content** over `pages`: the text is not stored twice,
and three triggers keep the index in step with the table. Those triggers are
not optional decoration — without them an external-content FTS5 table has no
rows to search, and every query returns nothing while every write reports
success.
"""

from __future__ import annotations

import logging
from typing import Any

from app.libsql_http import LibsqlClient, LibsqlError, ResultSet

logger = logging.getLogger(__name__)

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS sites (
        site_id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain TEXT UNIQUE NOT NULL,
        canonical_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pages (
        page_id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id INTEGER,
        url TEXT UNIQUE NOT NULL,
        title TEXT,
        description TEXT,
        body_text TEXT,
        quality_score REAL DEFAULT 1.0,
        digest TEXT,
        indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(site_id) REFERENCES sites(site_id)
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
        title,
        description,
        body_text,
        content='pages',
        content_rowid='page_id'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS crawl_queue (
        rowid INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE NOT NULL,
        domain TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        retry_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # --- Triggers that make external-content FTS5 actually work -------------
    """
    CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
        INSERT INTO page_fts(rowid, title, description, body_text)
        VALUES (new.page_id, new.title, new.description, new.body_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
        INSERT INTO page_fts(page_fts, rowid, title, description, body_text)
        VALUES ('delete', old.page_id, old.title, old.description, old.body_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
        INSERT INTO page_fts(page_fts, rowid, title, description, body_text)
        VALUES ('delete', old.page_id, old.title, old.description, old.body_text);
        INSERT INTO page_fts(rowid, title, description, body_text)
        VALUES (new.page_id, new.title, new.description, new.body_text);
    END
    """,
    # --- Indexes the query paths rely on -----------------------------------
    "CREATE INDEX IF NOT EXISTS idx_pages_site ON pages(site_id)",
    # Partial unique index: the database itself enforces SHA-256 deduplication
    # on body text, so two crawl workers racing on the same page cannot both
    # insert it and a bug in the pipeline cannot poison the index.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pages_digest ON pages(digest) "
    "WHERE digest IS NOT NULL AND digest <> ''",
    "CREATE INDEX IF NOT EXISTS idx_pages_indexed_at ON pages(indexed_at)",
    "CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status, rowid)",
    "CREATE INDEX IF NOT EXISTS idx_queue_domain ON crawl_queue(domain)",
)

COUNT_STATEMENTS: tuple[str, ...] = (
    "SELECT COUNT(*) FROM pages",
    "SELECT COUNT(*) FROM sites",
    "SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'",
)


def _scalar(result: ResultSet, default: int = 0) -> int:
    value = result.scalar(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def initialize_database(client: LibsqlClient) -> dict[str, int]:
    """Create every table, trigger and index; return what the index holds."""
    await client.batch([(sql, ()) for sql in SCHEMA_STATEMENTS])
    counts = await read_counts(client)
    logger.info(
        "schema ready: %d sites, %d pages, %d queued",
        counts["sites"],
        counts["pages"],
        counts["queued"],
    )
    return counts


async def read_counts(client: LibsqlClient) -> dict[str, int]:
    """The three integers the health and stats endpoints report."""
    results = await client.batch([(sql, ()) for sql in COUNT_STATEMENTS])
    return {
        "pages": _scalar(results[0]),
        "sites": _scalar(results[1]),
        "queued": _scalar(results[2]),
    }


async def verify_writable(client: LibsqlClient) -> bool:
    """Prove the database accepts writes before a crawl relies on it.

    A read-only token, the wrong branch or a staging database fails here,
    loudly, instead of silently during a crawl. The sentinel row is removed
    again, so the check leaves the index exactly as it found it.
    """
    sentinel = "https://verify.eyesearch.invalid/__writable_probe__"
    try:
        await client.batch(
            [
                ("BEGIN IMMEDIATE", ()),
                ("DELETE FROM pages WHERE url = ?", (sentinel,)),
                (
                    "INSERT INTO pages (site_id, url, title, description, body_text, digest) "
                    "VALUES (NULL, ?, ?, ?, ?, ?)",
                    (sentinel, "probe", "probe", "probe", "probe"),
                ),
                ("DELETE FROM pages WHERE url = ?", (sentinel,)),
                ("COMMIT", ()),
            ]
        )
    except LibsqlError as error:
        logger.error("database is not writable: %s", error)
        try:
            await client.execute("ROLLBACK")
        except LibsqlError:
            pass
        return False
    return True


async def probe(client: LibsqlClient) -> dict[str, Any]:
    """A one-shot health read used by `/health` and `--probe`."""
    try:
        counts = await read_counts(client)
    except LibsqlError as error:
        return {"db": "down", "error": str(error)}
    return {"db": "up", **counts}


__all__ = [
    "COUNT_STATEMENTS",
    "SCHEMA_STATEMENTS",
    "initialize_database",
    "probe",
    "read_counts",
    "verify_writable",
]
