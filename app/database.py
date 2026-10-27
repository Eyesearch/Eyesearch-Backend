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
        category TEXT DEFAULT 'general_reference',
        language TEXT DEFAULT 'en',
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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        processed_at TIMESTAMP
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

#: Columns added after their table was first created. Applied with ALTER, so a
#: database created by an earlier build picks them up without a rebuild.
COLUMN_ADDITIONS: tuple[tuple[str, str, str], ...] = (
    ("crawl_queue", "processed_at", "TIMESTAMP"),
    ("pages", "category", "TEXT DEFAULT 'general_reference'"),
    ("pages", "language", "TEXT DEFAULT 'en'"),
)

#: Statements that need a column from COLUMN_ADDITIONS to exist first.
POST_COLUMN_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_queue_processed ON crawl_queue(status, processed_at)",
    "CREATE INDEX IF NOT EXISTS idx_pages_category ON pages(category)",
    "CREATE INDEX IF NOT EXISTS idx_pages_language ON pages(language)",
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


#: Tables only the pre-rebuild backend created. Any of these present means the
#: database predates this schema.
LEGACY_TABLES: tuple[str, ...] = (
    "documents_fts",
    "documents",
    "search_records",
    "crawl_backlog",
    "page_fts",
    "pages",
    "sites",
    "crawl_queue",
)

class LegacySchemaError(RuntimeError):
    """The database holds a pre-rebuild schema and dropping it was not allowed."""


async def _table_columns(client: LibsqlClient, table: str) -> set[str]:
    """The column names of a table, or an empty set when it does not exist."""
    try:
        result = await client.execute(f"PRAGMA table_info({table})")
    except LibsqlError:
        return set()
    return {str(row[1]) for row in result.rows if len(row) > 1}


async def detect_legacy_schema(client: LibsqlClient) -> list[str]:
    """Name the legacy artefacts this database still holds, if any.

    `sites` and `crawl_queue` exist in both schemas, so their presence proves
    nothing on its own. The signals that do are a `pages` table without
    `body_text`, and the tables only the previous backend created.
    """
    found: list[str] = []

    page_columns = await _table_columns(client, "pages")
    legacy_pages = bool(page_columns) and "body_text" not in page_columns
    if legacy_pages:
        found.append("pages (pre-rebuild columns)")

    for table in ("documents", "documents_fts", "search_records", "crawl_backlog"):
        if await _table_columns(client, table):
            found.append(table)

    # These three belong to the old schema too, but only count as evidence
    # once the old `pages` shape or an old-only table has confirmed it.
    if legacy_pages:
        for table in ("page_fts", "sites", "crawl_queue"):
            if await _table_columns(client, table):
                found.append(table)

    return found


async def migrate_legacy_schema(
    client: LibsqlClient,
    *,
    allow_drop: bool = True,
) -> list[str]:
    """Clear a pre-rebuild schema so the new one can be created.

    `CREATE TABLE IF NOT EXISTS` cannot change an existing table, so a database
    left over from the previous backend would keep its old columns and every
    write would fail with a missing-column error. The old rows are a crawl
    cache in a shape this code cannot read, so they are dropped rather than
    migrated, and what was removed is logged.
    """
    found = await detect_legacy_schema(client)
    if not found:
        return []

    if not allow_drop:
        raise LegacySchemaError(
            "the database still holds the pre-rebuild schema ("
            + ", ".join(found)
            + "). Set MIGRATE_LEGACY_SCHEMA=true to drop it, or point the app at "
            "an empty database."
        )

    logger.warning(
        "pre-rebuild schema detected (%s) — dropping it so the new schema can be created",
        ", ".join(found),
    )

    statements: list[tuple[str, tuple]] = [("BEGIN IMMEDIATE", ())]
    for table in LEGACY_TABLES:
        statements.append((f"DROP TABLE IF EXISTS {table}", ()))
    statements.append(("COMMIT", ()))

    try:
        await client.batch(statements)
    except LibsqlError as error:
        logger.error("legacy schema drop failed: %s", error)
        try:
            await client.execute("ROLLBACK")
        except LibsqlError:
            pass
        raise

    logger.warning("legacy schema dropped: %s", ", ".join(found))
    return found


async def resync_fts(client: LibsqlClient) -> bool:
    """Rebuild the FTS index when it has drifted from `pages`.

    `page_fts` is external content, so its triggers issue a `'delete'` command
    against the index for every updated row. If the index never held those rows
    — a database whose `pages` were written before the triggers existed — that
    command fails with `database disk image is malformed`, and *any* update to
    `pages` breaks, including the subdomain normalisation below. Search would
    also quietly return nothing.

    `COUNT(*)` on an external-content FTS table reads the content table and so
    always matches; the shadow `page_fts_docsize` table is what actually counts
    indexed documents. If the shadow table is unavailable, the check is skipped
    rather than guessed at.
    """
    try:
        pages = await client.execute("SELECT COUNT(*) FROM pages")
        indexed = await client.execute("SELECT COUNT(*) FROM page_fts_docsize")
    except LibsqlError as error:
        logger.debug("could not compare the FTS index with pages: %s", error)
        return False

    page_count = int(pages.scalar(0) or 0)
    indexed_count = int(indexed.scalar(0) or 0)
    if page_count == indexed_count:
        return False

    logger.warning(
        "FTS index out of sync (%d pages, %d indexed) — rebuilding", page_count, indexed_count
    )
    try:
        await client.execute("INSERT INTO page_fts(page_fts) VALUES('rebuild')")
    except LibsqlError as error:
        logger.error("FTS rebuild failed: %s", error)
        return False
    return True


async def normalize_legacy_subdomains(client: LibsqlClient) -> int:
    """Merge subdomain rows in `sites` into their registrable root domain.

    Before `domain_of()` collapsed subdomains, `sites` and `crawl_queue` stored
    raw hosts, so `python.fossee.in` and `om.fossee.in` were separate rows and
    each carried its own page quota. This folds them together: the group keeps
    one `sites` row under the canonical domain, its pages are re-pointed to that
    row, and the redundant rows are deleted.

    Order matters and is enforced here — pages are re-pointed *before* their old
    site row is deleted, so a foreign key can never be left dangling. The whole
    thing is one transaction, and it is idempotent: a database that is already
    canonical produces no statements and no writes.

    Returns the number of redundant `sites` rows removed.
    """
    from app.agents import load_agent

    domain_of = load_agent("01_frontier_agent").domain_of

    try:
        sites = await client.execute("SELECT site_id, domain FROM sites")
    except LibsqlError as error:
        # Best-effort: a database blip here must not stop the service booting.
        logger.warning("skipping subdomain normalisation: %s", error)
        return 0
    if not sites.rows:
        return 0

    domain_by_id: dict[int, str] = {}
    by_canonical: dict[str, list[int]] = {}
    for row in sites.rows:
        site_id = int(row[0])
        raw = str(row[1] or "")
        domain_by_id[site_id] = raw
        canonical = domain_of(f"https://{raw}/") or raw
        by_canonical.setdefault(canonical, []).append(site_id)

    statements: list[tuple[str, tuple]] = [("BEGIN IMMEDIATE", ())]
    merged = 0
    renamed = 0

    for canonical, site_ids in by_canonical.items():
        if len(site_ids) == 1 and domain_by_id[site_ids[0]] == canonical:
            continue

        # Prefer a row that already holds the canonical domain, so its id and
        # any reference to it survive; otherwise promote the lowest id.
        matching = [site_id for site_id in site_ids if domain_by_id[site_id] == canonical]
        target = matching[0] if matching else min(site_ids)
        redundant = [site_id for site_id in site_ids if site_id != target]

        if domain_by_id[target] != canonical:
            statements.append(
                ("UPDATE sites SET domain = ? WHERE site_id = ?", (canonical, target))
            )
            renamed += 1

        if redundant:
            placeholders = ", ".join("?" for _ in redundant)
            statements.append(
                (
                    f"UPDATE pages SET site_id = ? WHERE site_id IN ({placeholders})",
                    (target, *redundant),
                )
            )
            statements.append(
                (f"DELETE FROM sites WHERE site_id IN ({placeholders})", tuple(redundant))
            )
            merged += len(redundant)

    try:
        queued = await client.execute("SELECT DISTINCT domain FROM crawl_queue")
    except LibsqlError as error:
        logger.warning("skipping queue domain normalisation: %s", error)
        return 0
    queue_updates = 0
    for row in queued.rows:
        raw = str(row[0] or "")
        canonical = domain_of(f"https://{raw}/") or raw
        if canonical != raw:
            statements.append(
                ("UPDATE crawl_queue SET domain = ? WHERE domain = ?", (canonical, raw))
            )
            queue_updates += 1

    if len(statements) == 1:  # only BEGIN: nothing to do
        return 0

    statements.append(("COMMIT", ()))
    try:
        await client.batch(statements)
    except LibsqlError as error:
        logger.error("subdomain normalisation failed: %s", error)
        try:
            await client.execute("ROLLBACK")
        except LibsqlError:
            pass
        return 0

    logger.info(
        "normalised legacy subdomains: %d site rows merged, %d renamed, "
        "%d queue domains updated",
        merged,
        renamed,
        queue_updates,
    )
    return merged


async def initialize_database(
    client: LibsqlClient,
    *,
    migrate: bool = True,
    allow_legacy_drop: bool = True,
) -> dict[str, int]:
    """Create every table, trigger and index; return what the index holds."""
    if migrate:
        await migrate_legacy_schema(client, allow_drop=allow_legacy_drop)

    await client.batch([(sql, ()) for sql in SCHEMA_STATEMENTS])
    await ensure_columns(client)
    await client.batch([(sql, ()) for sql in POST_COLUMN_STATEMENTS])
    # Before anything updates `pages`: a stale index makes every update fail.
    await resync_fts(client)
    await normalize_legacy_subdomains(client)

    # Rows queued before their domain reached its cap would otherwise sit at
    # 'pending' for ever and make the frontier look full.
    from app.engine.store import IndexStore

    try:
        await IndexStore(client).skip_pending_for_capped_domains()
    except LibsqlError as error:
        logger.warning("skipping capped-domain reconciliation: %s", error)
    counts = await read_counts(client)
    logger.info(
        "schema ready: %d sites, %d pages, %d queued",
        counts["sites"],
        counts["pages"],
        counts["queued"],
    )
    return counts


async def ensure_columns(client: LibsqlClient) -> list[str]:
    """Add any column a later build introduced to an existing table."""
    added: list[str] = []

    for table, column, declaration in COLUMN_ADDITIONS:
        columns = await _table_columns(client, table)
        if not columns or column in columns:
            continue
        await client.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        added.append(f"{table}.{column}")

    if added:
        logger.info("migration applied: %s", ", ".join(added))
    return added


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
    "COLUMN_ADDITIONS",
    "COUNT_STATEMENTS",
    "normalize_legacy_subdomains",
    "resync_fts",
    "SCHEMA_STATEMENTS",
    "initialize_database",
    "probe",
    "read_counts",
    "verify_writable",
]
