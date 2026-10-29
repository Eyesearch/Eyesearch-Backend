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


# ---------------------------------------------------------------------------
# Legacy subdomain normalisation
# ---------------------------------------------------------------------------


async def test_normalize_legacy_subdomains_merges_duplicates(client):
    from app.database import normalize_legacy_subdomains

    await client.batch(
        [
            (
                "INSERT INTO sites (domain, canonical_url) VALUES ('python.fossee.in', 'https://python.fossee.in/')",
                (),
            ),
            (
                "INSERT INTO sites (domain, canonical_url) VALUES ('om.fossee.in', 'https://om.fossee.in/')",
                (),
            ),
        ]
    )
    sites = await client.execute("SELECT site_id, domain FROM sites ORDER BY site_id")
    first_id, second_id = int(sites.rows[0][0]), int(sites.rows[1][0])

    await client.execute(
        "INSERT INTO pages (site_id, url, title, description, body_text, digest) "
        "VALUES (?, 'https://python.fossee.in/1', 'T1', 'D1', 'B1', 'h1')",
        (first_id,),
    )
    await client.execute(
        "INSERT INTO pages (site_id, url, title, description, body_text, digest) "
        "VALUES (?, 'https://om.fossee.in/1', 'T2', 'D2', 'B2', 'h2')",
        (second_id,),
    )
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status) "
        "VALUES ('https://r.fossee.in/x', 'r.fossee.in', 'pending')"
    )

    merged = await normalize_legacy_subdomains(client)

    assert merged >= 1

    canonical = await client.execute("SELECT site_id, domain FROM sites WHERE domain = 'fossee.in'")
    assert len(canonical.rows) == 1
    canonical_id = int(canonical.rows[0][0])

    pages = await client.execute("SELECT site_id FROM pages WHERE url LIKE '%fossee.in%'")
    assert [int(row[0]) for row in pages.rows] == [canonical_id, canonical_id]

    queue = await client.execute(
        "SELECT domain FROM crawl_queue WHERE url = 'https://r.fossee.in/x'"
    )
    assert queue.scalar() == "fossee.in"


async def test_normalisation_is_idempotent(client):
    from app.database import normalize_legacy_subdomains

    await client.execute(
        "INSERT INTO sites (domain, canonical_url) VALUES ('a.fossee.in', 'https://a.fossee.in/')"
    )
    await client.execute(
        "INSERT INTO sites (domain, canonical_url) VALUES ('b.fossee.in', 'https://b.fossee.in/')"
    )

    first = await normalize_legacy_subdomains(client)
    second = await normalize_legacy_subdomains(client)
    third = await normalize_legacy_subdomains(client)

    assert first == 1
    assert second == 0
    assert third == 0

    sites = await client.execute("SELECT COUNT(*) FROM sites")
    assert int(sites.scalar(0)) == 1


async def test_normalisation_leaves_a_canonical_database_alone(client):
    from app.database import normalize_legacy_subdomains

    await client.execute(
        "INSERT INTO sites (domain, canonical_url) VALUES ('karnataka.gov.in', 'https://karnataka.gov.in/')"
    )
    await client.execute(
        "INSERT INTO sites (domain, canonical_url) VALUES ('thehindu.com', 'https://thehindu.com/')"
    )

    assert await normalize_legacy_subdomains(client) == 0

    sites = await client.execute("SELECT domain FROM sites ORDER BY domain")
    assert [row[0] for row in sites.rows] == ["karnataka.gov.in", "thehindu.com"]


async def test_normalisation_keeps_an_existing_canonical_row(client):
    """If the root row already exists, subdomains fold into it, not the reverse."""
    from app.database import normalize_legacy_subdomains

    await client.execute(
        "INSERT INTO sites (domain, canonical_url) VALUES ('fossee.in', 'https://fossee.in/')"
    )
    canonical_id = int(
        (await client.execute("SELECT site_id FROM sites WHERE domain = 'fossee.in'")).scalar(0)
    )
    await client.execute(
        "INSERT INTO sites (domain, canonical_url) VALUES ('python.fossee.in', 'https://python.fossee.in/')"
    )
    sub_id = int(
        (
            await client.execute(
                "SELECT site_id FROM sites WHERE domain = 'python.fossee.in'"
            )
        ).scalar(0)
    )
    await client.execute(
        "INSERT INTO pages (site_id, url, title, description, body_text, digest) "
        "VALUES (?, 'https://python.fossee.in/1', 'T', 'D', 'B', 'h-sub')",
        (sub_id,),
    )

    await normalize_legacy_subdomains(client)

    remaining = await client.execute("SELECT site_id, domain FROM sites")
    assert len(remaining.rows) == 1
    assert int(remaining.rows[0][0]) == canonical_id

    page = await client.execute("SELECT site_id FROM pages WHERE digest = 'h-sub'")
    assert int(page.scalar(0)) == canonical_id


async def test_normalisation_does_not_orphan_pages(client):
    """The foreign key must still resolve after the merge."""
    from app.database import normalize_legacy_subdomains

    await client.execute(
        "INSERT INTO sites (domain, canonical_url) VALUES ('x.iiita.ac.in', 'https://x.iiita.ac.in/')"
    )
    site_id = int((await client.execute("SELECT site_id FROM sites")).scalar(0))
    await client.execute(
        "INSERT INTO pages (site_id, url, title, description, body_text, digest) "
        "VALUES (?, 'https://x.iiita.ac.in/1', 'T', 'D', 'B', 'h-orphan')",
        (site_id,),
    )

    await normalize_legacy_subdomains(client)

    orphans = await client.execute(
        "SELECT COUNT(*) FROM pages p LEFT JOIN sites s ON s.site_id = p.site_id "
        "WHERE p.site_id IS NOT NULL AND s.site_id IS NULL"
    )
    assert int(orphans.scalar(0)) == 0

    domain = await client.execute(
        "SELECT s.domain FROM pages p JOIN sites s ON s.site_id = p.site_id "
        "WHERE p.digest = 'h-orphan'"
    )
    assert domain.scalar() == "iiita.ac.in"


async def test_normalisation_handles_an_empty_database(client):
    from app.database import normalize_legacy_subdomains

    assert await normalize_legacy_subdomains(client) == 0


async def test_startup_normalises_legacy_rows(client):
    """initialize_database is what runs on boot, so it must do the merge."""
    await client.batch(
        [
            (
                "INSERT INTO sites (domain, canonical_url) VALUES ('a.fossee.in', 'https://a.fossee.in/')",
                (),
            ),
            (
                "INSERT INTO sites (domain, canonical_url) VALUES ('b.fossee.in', 'https://b.fossee.in/')",
                (),
            ),
        ]
    )

    counts = await initialize_database(client, migrate=False)

    assert counts["sites"] == 1
    remaining = await client.execute("SELECT domain FROM sites")
    assert remaining.scalar() == "fossee.in"


async def test_a_failed_normalisation_rolls_back(client):
    """A failure mid-merge must leave the database exactly as it was."""
    from app.database import normalize_legacy_subdomains
    from app.libsql_http import LibsqlError

    await client.batch(
        [
            (
                "INSERT INTO sites (domain, canonical_url) VALUES ('a.fossee.in', 'https://a.fossee.in/')",
                (),
            ),
            (
                "INSERT INTO sites (domain, canonical_url) VALUES ('b.fossee.in', 'https://b.fossee.in/')",
                (),
            ),
        ]
    )

    class _Exploding:
        def __init__(self, inner):
            self.inner = inner

        async def execute(self, sql, args=()):
            return await self.inner.execute(sql, args)

        async def batch(self, statements):
            if any("DELETE FROM sites" in sql for sql, _ in statements):
                raise LibsqlError("boom (statement: DELETE FROM sites)")
            return await self.inner.batch(statements)

        async def close(self):
            await self.inner.close()

    merged = await normalize_legacy_subdomains(_Exploding(client))

    assert merged == 0
    sites = await client.execute("SELECT COUNT(*) FROM sites")
    assert int(sites.scalar(0)) == 2


# ---------------------------------------------------------------------------
# FTS resync
# ---------------------------------------------------------------------------


async def test_a_missing_search_vector_is_backfilled(client):
    """PostgreSQL keeps search_vector in a trigger, so the failure mode is a row
    that predates the trigger rather than a separate index that drifted."""
    from app.database import resync_fts

    await client.batch(
        [
            (
                "INSERT INTO pages (site_id, url, title, description, body_text, digest) "
                f"VALUES (NULL, 'https://a.gov.in/{i}', 'Karnataka {i}', 'D', 'B', 'dg{i}')",
                (),
            )
            for i in range(3)
        ]
    )
    # Simulate rows written before the trigger existed.
    await client.execute("UPDATE pages SET search_vector = NULL")
    before = await client.execute(
        "SELECT COUNT(*) FROM pages WHERE search_vector IS NOT NULL"
    )
    assert int(before.scalar(0)) == 0

    rebuilt = await resync_fts(client)

    assert rebuilt is True
    after = await client.execute(
        "SELECT COUNT(*) FROM pages WHERE search_vector IS NOT NULL"
    )
    assert int(after.scalar(0)) == 3


async def test_a_missing_vector_makes_a_page_unfindable_until_backfilled(client):
    from app.database import resync_fts

    await client.execute(
        "INSERT INTO pages (site_id, url, title, description, body_text, digest) "
        "VALUES (NULL, 'https://a.gov.in/1', 'Karnataka', 'D', 'B', 'dg1')"
    )
    await client.execute("UPDATE pages SET search_vector = NULL")

    # This is the failure the rebuild exists for: the row is there but invisible.
    missing = await client.execute(
        "SELECT COUNT(*) FROM pages "
        "WHERE search_vector @@ to_tsquery('simple', 'karnataka')"
    )
    assert int(missing.scalar(0)) == 0

    await resync_fts(client)

    found = await client.execute(
        "SELECT COUNT(*) FROM pages "
        "WHERE search_vector @@ to_tsquery('simple', 'karnataka')"
    )
    assert int(found.scalar(0)) == 1


async def test_a_synced_database_needs_no_backfill(client):
    from app.database import resync_fts

    await client.execute(
        "INSERT INTO pages (site_id, url, title, description, body_text, digest) "
        "VALUES (NULL, 'https://a.gov.in/1', 'Karnataka', 'D', 'B', 'dg1')"
    )

    assert await resync_fts(client) is False


async def test_an_empty_database_needs_no_backfill(client):
    from app.database import resync_fts

    assert await resync_fts(client) is False
