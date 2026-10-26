"""Two-tier schema: canonical `sites`, bounded `pages`, external-content FTS5.

One row in `sites` per domain; one row in `pages` per root or sitelink page,
bounded to a handful per site. `page_fts` is FTS5 over `pages` with
`content='pages'`, so the text is not stored twice, and three triggers keep the
index in step with the table.

The previous flat `documents` model (and the legacy result cache) is dropped at
startup: it was unstructured, it could hold dozens of rows for one domain, and
this schema supersedes it.
"""

from __future__ import annotations

# The seven indexed columns of page_fts, in bm25 weight order.
FTS_COLUMNS: tuple[str, ...] = (
    "title",
    "headings",
    "description",
    "nav_terms",
    "site_terms",
    "category_terms",
    "url_terms",
)

# bm25 weights, matching FTS_COLUMNS one-for-one.
BM25_WEIGHTS: tuple[float, ...] = (10.0, 5.0, 2.5, 3.0, 4.0, 1.0, 2.0)

SITES_TABLE = """
CREATE TABLE IF NOT EXISTS sites (
    site_id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL UNIQUE,
    canonical_url TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    authority_score REAL NOT NULL DEFAULT 1.0,
    last_crawled_at INTEGER
)
"""

PAGES_TABLE = """
CREATE TABLE IF NOT EXISTS pages (
    page_id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
    url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('root', 'sitelink')),
    title TEXT NOT NULL DEFAULT '',
    headings TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    nav_terms TEXT NOT NULL DEFAULT '',
    site_terms TEXT NOT NULL DEFAULT '',
    category_terms TEXT NOT NULL DEFAULT '',
    url_terms TEXT NOT NULL DEFAULT '',
    http_status INTEGER NOT NULL DEFAULT 200,
    noindex INTEGER NOT NULL DEFAULT 0,
    canonical_url TEXT,
    content_hash TEXT,
    last_crawled_at INTEGER
)
"""

PAGE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
    title,
    headings,
    description,
    nav_terms,
    site_terms,
    category_terms,
    url_terms,
    content='pages',
    content_rowid='page_id',
    tokenize='unicode61 remove_diacritics 2',
    detail=full
)
"""

PAGE_TRIGGERS: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
        INSERT INTO page_fts(rowid, title, headings, description, nav_terms, site_terms, category_terms, url_terms)
        VALUES (new.page_id, new.title, new.headings, new.description, new.nav_terms, new.site_terms, new.category_terms, new.url_terms);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
        INSERT INTO page_fts(page_fts, rowid, title, headings, description, nav_terms, site_terms, category_terms, url_terms)
        VALUES ('delete', old.page_id, old.title, old.headings, old.description, old.nav_terms, old.site_terms, old.category_terms, old.url_terms);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
        INSERT INTO page_fts(page_fts, rowid, title, headings, description, nav_terms, site_terms, category_terms, url_terms)
        VALUES ('delete', old.page_id, old.title, old.headings, old.description, old.nav_terms, old.site_terms, old.category_terms, old.url_terms);
        INSERT INTO page_fts(rowid, title, headings, description, nav_terms, site_terms, category_terms, url_terms)
        VALUES (new.page_id, new.title, new.headings, new.description, new.nav_terms, new.site_terms, new.category_terms, new.url_terms);
    END
    """,
)

CRAWL_TABLES: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS crawl_backlog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'query',
        reason TEXT,
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
    SITES_TABLE,
    PAGES_TABLE,
    "CREATE INDEX IF NOT EXISTS idx_pages_site ON pages(site_id, kind)",
    "CREATE INDEX IF NOT EXISTS idx_pages_url ON pages(url)",
    "CREATE INDEX IF NOT EXISTS idx_sites_domain ON sites(domain)",
    "CREATE INDEX IF NOT EXISTS idx_sites_category ON sites(category)",
    PAGE_FTS,
    *PAGE_TRIGGERS,
    *CRAWL_TABLES,
)

# Tables from earlier releases. They are derived data, and the brief asks for a
# clean recreation, so they are dropped at startup.
LEGACY_TABLES: tuple[str, ...] = (
    "documents_fts",
    "documents",
    "search_records",
)

LEGACY_TRIGGERS: tuple[str, ...] = (
    "documents_ai",
    "documents_ad",
    "documents_au",
)

# Columns added to an existing database after the first release.
ENGINE_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("crawl_backlog", "kind TEXT NOT NULL DEFAULT 'query'"),
    ("crawl_backlog", "reason TEXT"),
)

__all__ = [
    "ENGINE_MIGRATIONS",
    "ENGINE_SCHEMA",
    "LEGACY_TABLES",
    "LEGACY_TRIGGERS",
    "FTS_COLUMNS",
    "BM25_WEIGHTS",
    "SITES_TABLE",
    "PAGES_TABLE",
    "PAGE_FTS",
    "PAGE_TRIGGERS",
    "CRAWL_TABLES",
]
