"""Schema for the proprietary index.

`documents` is the canonical store (one row per normalised URL, capped at 2 KB).
`documents_fts` is the FTS5 table the task specifies — it duplicates the
searchable text so BM25 works without touching the row store. `crawl_backlog`
holds queries the index could not answer; `crawl_queue` holds URLs to fetch.
"""

from __future__ import annotations

# The exact virtual table from the brief.
FTS_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    url, title, snippet, content, language, updated_at UNINDEXED
)
"""

ENGINE_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS documents (
        url TEXT PRIMARY KEY,
        canonical_url TEXT,
        domain TEXT,
        root_domain TEXT,
        domain_class TEXT DEFAULT 'static',
        title TEXT,
        snippet TEXT,
        content TEXT,
        language TEXT DEFAULT 'en',
        favicon_url TEXT,
        payload_bytes INTEGER DEFAULT 0,
        content_hash TEXT,
        etag TEXT,
        last_modified TEXT,
        last_checked_at TIMESTAMP,
        http_status INTEGER,
        fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        quality REAL DEFAULT 0.5
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_documents_domain ON documents(domain)",
    "CREATE INDEX IF NOT EXISTS idx_documents_root ON documents(root_domain)",
    "CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(updated_at)",
    FTS_TABLE,
    """
    CREATE TABLE IF NOT EXISTS crawl_backlog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT NOT NULL,
        hits INTEGER DEFAULT 0,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        processed_at TIMESTAMP
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_backlog_query ON crawl_backlog(query)",
    """
    CREATE TABLE IF NOT EXISTS crawl_queue (
        url TEXT PRIMARY KEY,
        domain TEXT,
        depth INTEGER DEFAULT 0,
        reason TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        processed_at TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status)",
)

# Columns added after the first release; applied to existing databases so an
# upgrade does not need a manual migration step.
ENGINE_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("documents", "root_domain TEXT"),
    ("documents", "domain_class TEXT DEFAULT 'static'"),
    ("documents", "payload_bytes INTEGER DEFAULT 0"),
    ("documents", "content_hash TEXT"),
    ("documents", "etag TEXT"),
    ("documents", "last_modified TEXT"),
    ("documents", "last_checked_at TIMESTAMP"),
)

__all__ = ["ENGINE_SCHEMA", "ENGINE_MIGRATIONS", "FTS_TABLE"]
