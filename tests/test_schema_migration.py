"""Clearing a pre-rebuild database.

`CREATE TABLE IF NOT EXISTS` cannot change a table that already exists. The
previous backend shipped a different `pages` and `sites`, so a warm database
would keep the old columns and every write would fail on a missing column —
silently, once per crawl batch. These tests pin the detection and the drop.
"""

from __future__ import annotations

import pytest

from app.database import (
    LegacySchemaError,
    detect_legacy_schema,
    initialize_database,
    migrate_legacy_schema,
)
from app.libsql_http import LocalSqliteClient

LEGACY_DDL = """
CREATE TABLE sites (
    site_id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL UNIQUE,
    canonical_url TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    authority_score REAL NOT NULL DEFAULT 1.0,
    last_crawled_at INTEGER
);
CREATE TABLE pages (
    page_id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL,
    url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    headings TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    content_hash TEXT,
    last_crawled_at INTEGER
);
CREATE TABLE crawl_backlog (
    query TEXT PRIMARY KEY,
    hits INTEGER DEFAULT 0
);
INSERT INTO sites (domain, canonical_url, name, category) VALUES ('old.gov.in', 'https://old.gov.in/', 'Old', 'gov');
INSERT INTO pages (site_id, url, kind, title, content, content_hash)
VALUES (1, 'https://old.gov.in/', 'root', 'Old Portal', 'old text', 'hash-1');
"""


async def _legacy_database() -> LocalSqliteClient:
    client = LocalSqliteClient(":memory:")
    for statement in [part.strip() for part in LEGACY_DDL.split(";") if part.strip()]:
        await client.execute(statement)
    return client


async def test_a_clean_database_has_nothing_to_migrate(client):
    assert await detect_legacy_schema(client) == []
    assert await migrate_legacy_schema(client) == []


async def test_a_pre_rebuild_database_is_detected():
    legacy = await _legacy_database()
    try:
        found = await detect_legacy_schema(legacy)
    finally:
        await legacy.close()

    assert any("pages" in item for item in found)
    assert "crawl_backlog" in found


async def test_the_legacy_schema_is_dropped_and_the_new_one_created():
    legacy = await _legacy_database()
    try:
        dropped = await migrate_legacy_schema(legacy)

        assert dropped

        counts = await initialize_database(legacy, migrate=False)
        assert counts == {"pages": 0, "sites": 0, "queued": 0}

        columns = await legacy.execute("PRAGMA table_info(pages)")
        names = {str(row[1]) for row in columns.rows}
        assert "body_text" in names
        assert "content_hash" not in names

        # The old crawl cache is gone, not half-migrated.
        pages = await legacy.execute("SELECT COUNT(*) FROM pages")
        assert int(pages.scalar(0)) == 0
    finally:
        await legacy.close()


async def test_initialize_database_migrates_on_its_own():
    legacy = await _legacy_database()
    try:
        await initialize_database(legacy)

        columns = await legacy.execute("PRAGMA table_info(sites)")
        names = {str(row[1]) for row in columns.rows}
        assert "created_at" in names
        assert "authority_score" not in names
    finally:
        await legacy.close()


async def test_the_drop_can_be_refused_instead():
    legacy = await _legacy_database()
    try:
        with pytest.raises(LegacySchemaError):
            await migrate_legacy_schema(legacy, allow_drop=False)

        # Refusing must leave the database untouched.
        pages = await legacy.execute("SELECT COUNT(*) FROM pages")
        assert int(pages.scalar(0)) == 1
    finally:
        await legacy.close()


async def test_initialize_database_refuses_when_the_drop_is_disabled():
    legacy = await _legacy_database()
    try:
        with pytest.raises(LegacySchemaError):
            await initialize_database(legacy, allow_legacy_drop=False)
    finally:
        await legacy.close()
