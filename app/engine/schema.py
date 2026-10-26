"""Schema for the proprietary index.

`documents` is the single source of truth: one row per normalised URL, carrying
the clean title, a 3–4 sentence summary, the canonical URL, the domain and the
category the page was crawled under.

`documents_fts` is an FTS5 table over `documents` (external content, so the text
is not stored twice) with the unicode61 tokenizer. Three triggers keep the index
in perfect sync with the table, which means application code never writes to the
FTS table directly — no half-synced rows are possible.

Everything is `CREATE ... IF NOT EXISTS`, so a cold start on an empty Turso
database rebuilds the whole schema by itself.
"""

from __future__ import annotations

# Columns indexed by FTS5, in bm25 weight order: title, snippet, content, url.
# The weights in app/engine/store.py follow this order exactly.
FTS_COLUMNS: tuple[str, ...] = ("title", "snippet", "content", "url")

DOCUMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS documents (
    url TEXT PRIMARY KEY,
    title TEXT,
    snippet TEXT,
    content TEXT,
    domain TEXT,
    category TEXT,
    canonical_url TEXT,
    root_domain TEXT,
    domain_class TEXT DEFAULT 'static',
    language TEXT DEFAULT 'en',
    favicon_url TEXT,
    payload_bytes INTEGER DEFAULT 0,
    content_hash TEXT,
    etag TEXT,
    last_modified TEXT,
    last_checked_at TIMESTAMP,
    http_status INTEGER,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    quality REAL DEFAULT 0.5
)
"""

# External-content FTS5: the text lives in `documents`, the index references it.
FTS_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, snippet, content, url,
    content='documents',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
)
"""

# The three triggers from the FTS5 documentation, verbatim in shape.
FTS_TRIGGERS: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
        INSERT INTO documents_fts(rowid, title, snippet, content, url)
        VALUES (new.rowid, new.title, new.snippet, new.content, new.url);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
        INSERT INTO documents_fts(documents_fts, rowid, title, snippet, content, url)
        VALUES ('delete', old.rowid, old.title, old.snippet, old.content, old.url);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
        INSERT INTO documents_fts(documents_fts, rowid, title, snippet, content, url)
        VALUES ('delete', old.rowid, old.title, old.snippet, old.content, old.url);
        INSERT INTO documents_fts(rowid, title, snippet, content, url)
        VALUES (new.rowid, new.title, new.snippet, new.content, new.url);
    END
    """,
)

CRAWL_TABLES: tuple[str, ...] = (
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
        category TEXT,
        depth INTEGER DEFAULT 0,
        reason TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        processed_at TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status)",
    "CREATE INDEX IF NOT EXISTS idx_queue_category ON crawl_queue(category, status)",
)

ENGINE_SCHEMA: tuple[str, ...] = (
    DOCUMENTS_TABLE,
    "CREATE INDEX IF NOT EXISTS idx_documents_domain ON documents(domain)",
    "CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category)",
    "CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(updated_at)",
    FTS_TABLE,
    *FTS_TRIGGERS,
    *CRAWL_TABLES,
)

# Columns added after the first release; applied to existing databases.
ENGINE_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("documents", "category TEXT"),
    ("documents", "root_domain TEXT"),
    ("documents", "domain_class TEXT DEFAULT 'static'"),
    ("documents", "payload_bytes INTEGER DEFAULT 0"),
    ("documents", "content_hash TEXT"),
    ("documents", "etag TEXT"),
    ("documents", "last_modified TEXT"),
    ("documents", "last_checked_at TIMESTAMP"),
    ("crawl_queue", "category TEXT"),
)

__all__ = [
    "ENGINE_SCHEMA",
    "ENGINE_MIGRATIONS",
    "FTS_TABLE",
    "FTS_TRIGGERS",
    "FTS_COLUMNS",
    "DOCUMENTS_TABLE",
    "CRAWL_TABLES",
]
