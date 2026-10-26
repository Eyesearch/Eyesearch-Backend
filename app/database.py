"""Turso (LibSQL) access layer.

One async client created in ``lifespan`` and reused for the process lifetime;
no per-request connections, no blocking driver on the request path. Every public
function is typed, bounded by ``DB_TIMEOUT_S``, and raises ``DatabaseError``
rather than leaking driver exceptions into a route.

Local development works with no Turso account: ``TURSO_DB_URL`` defaults to
``file:./local.db`` and ``libsql_client`` serves that from local SQLite.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

import libsql_client

from app.config import Settings, get_settings
from app.engine.schema import ENGINE_SCHEMA
from app.errors import DatabaseError

# What a database URL should look like once we are done with it: no token, no
# credentials, safe to put in a log line or an HTTP response.
_SENSITIVE_PARAMS = ("authToken", "auth_token", "token", "password")

# Turso's cloud endpoint speaks Hrana over HTTP. libsql_client picks its backend
# from the URL scheme, and `libsql://`, `wss://` and `ws://` all select the
# WebSocket client — which Turso rejects with `400 Invalid response status`.
# Only `https://` selects the HTTP client, so remote URLs are rewritten here.
_SCHEME_REWRITES: tuple[tuple[str, str], ...] = (
    ("libsql://", "https://"),
    ("libsql+https://", "https://"),
    ("wss://", "https://"),
    ("ws://", "http://"),
)


def normalise_url_scheme(url: str) -> str:
    """Rewrite a remote database URL to the scheme the HTTP backend needs.

    `libsql://` and `wss://` both mean "WebSocket" to libsql_client, and Turso's
    cloud endpoint answers those with HTTP 400. `file:` URLs are left untouched.
    """
    cleaned = (url or "").strip()
    if not cleaned or cleaned.startswith("file:"):
        return cleaned
    lowered = cleaned.lower()
    for prefix, replacement in _SCHEME_REWRITES:
        if lowered.startswith(prefix):
            return f"{replacement}{cleaned[len(prefix):]}"
    return cleaned

logger = logging.getLogger(__name__)

# The schema from the task brief, plus additive indexes. Applied idempotently at
# startup so Render needs no separate migration step.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS search_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query_key TEXT NOT NULL,
        query TEXT NOT NULL,
        title TEXT,
        url TEXT NOT NULL,
        snippet TEXT,
        poster_url TEXT,
        source TEXT DEFAULT 'live_crawl',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (query_key, url)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_query_key_created ON search_records(query_key, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_created_at ON search_records(created_at)",
)
# ENGINE_SCHEMA (documents, documents_fts, crawl_backlog, crawl_queue) is applied
# in the same batch, so a cold start always has a usable index.

_UPSERT_SQL = """
INSERT INTO search_records (query_key, query, title, url, snippet, poster_url, source, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(query_key, url) DO UPDATE SET
    title = excluded.title,
    snippet = excluded.snippet,
    poster_url = excluded.poster_url,
    source = excluded.source,
    created_at = excluded.created_at
"""

_SELECT_CACHED_SQL = """
SELECT id, query_key, query, title, url, snippet, poster_url, source, created_at
FROM search_records
WHERE query_key = ? AND created_at >= ?
ORDER BY id ASC
LIMIT ?
"""

_COUNT_SQL = "SELECT COUNT(*) AS n FROM search_records WHERE query_key = ? AND created_at >= ?"
_PURGE_SQL = "DELETE FROM search_records WHERE created_at < ?"

_client: libsql_client.Client | None = None


def normalise_query(query: str) -> str:
    """The one normal form used for cache keys: lower-cased, whitespace-collapsed."""
    return " ".join((query or "").split()).lower()


def query_cache_key(query: str) -> str:
    """Stable cache key for a query: a hash of its normal form.

    Hashing (rather than storing the raw string) means two queries can never be
    confused by case, spacing or trailing punctuation, and the key is a fixed
    width regardless of what the user typed.
    """
    digest = hashlib.sha256(normalise_query(query).encode("utf-8")).hexdigest()
    return digest[:40]


def _timestamp(value: datetime | None = None) -> str:
    """Turso stores ``CURRENT_TIMESTAMP`` as ``YYYY-MM-DD HH:MM:SS`` in UTC."""
    moment = value or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _cutoff(ttl_hours: int) -> str:
    return _timestamp(datetime.now(timezone.utc) - timedelta(hours=max(0, ttl_hours)))


def _rows_to_dicts(result: Any) -> list[dict[str, Any]]:
    columns: Sequence[str] = tuple(getattr(result, "columns", ()) or ())
    rows: Iterable[Any] = getattr(result, "rows", ()) or ()
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(dict(row))
        elif columns:
            out.append({name: row[idx] for idx, name in enumerate(columns)})
        else:
            out.append({})
    return out


def safe_url(url: str) -> str:
    """The URL with any credentials or token parameters removed."""
    cleaned = url or ""
    for param in _SENSITIVE_PARAMS:
        if f"{param}=" in cleaned:
            head, _, tail = cleaned.partition(f"{param}=")
            remainder = tail.split("&", 1)[1] if "&" in tail else ""
            cleaned = f"{head}{param}=***{'&' + remainder if remainder else ''}"
    if "@" in cleaned and "://" in cleaned:
        scheme, _, rest = cleaned.partition("://")
        rest = rest.split("@", 1)[1]
        cleaned = f"{scheme}://{rest}"
    return cleaned


def describe_target(settings: Settings | None = None) -> dict[str, str]:
    """Where this process actually reads and writes. Safe to log and to expose."""
    cfg = settings or get_settings()
    effective = normalise_url_scheme(cfg.turso_db_url)
    return {
        "kind": "remote" if cfg.is_remote_db else "local",
        "url": safe_url(effective),
        "scheme": urlparse(effective).scheme or ("file" if effective.startswith("file:") else ""),
        "has_token": "yes" if bool(cfg.turso_auth_token) else "no",
    }


async def connect(settings: Settings | None = None) -> libsql_client.Client:
    """Create the shared client and prove it works. Called once from ``lifespan``."""
    global _client
    cfg = settings or get_settings()
    if _client is not None:
        return _client

    target = describe_target(cfg)

    # A remote URL without a token cannot work. Say so instead of silently
    # degrading to a database nobody is writing to.
    if cfg.is_remote_db and not cfg.turso_auth_token:
        message = (
            f"TURSO_AUTH_TOKEN is required for the remote database {target['url']}. "
            "Set TURSO_AUTH_TOKEN (and TURSO_DB_URL/TURSO_DATABASE_URL) in the service "
            "environment, or point TURSO_DB_URL at a file: path for local development."
        )
        logger.error(message)
        raise DatabaseError(message)

    effective_url = normalise_url_scheme(cfg.turso_db_url)
    if effective_url != cfg.turso_db_url.strip():
        logger.info(
            "database URL scheme normalised: %s → %s (Turso requires Hrana over HTTPS)",
            safe_url(cfg.turso_db_url),
            safe_url(effective_url),
        )

    kwargs: dict[str, Any] = {"url": effective_url}
    if cfg.turso_auth_token:
        # libsql_client sends this as `Authorization: Bearer <token>`.
        kwargs["auth_token"] = cfg.turso_auth_token
    try:
        _client = libsql_client.create_client(**kwargs)
    except Exception as exc:
        logger.error("database client creation failed for %s: %s", target["url"], exc)
        raise DatabaseError(f"could not create database client: {exc}") from exc

    client_class = type(_client).__name__
    if cfg.is_remote_db:
        logger.info("Connected to Remote Turso DB: %s (%s)", target["url"], client_class)
        if client_class == "HranaClient":
            # The WebSocket backend: Turso's cloud answers it with HTTP 400.
            logger.error(
                "database client is WebSocket-based (%s) for %s; Turso cloud needs the HTTP "
                "backend. Check that the URL scheme is https://.",
                client_class,
                target["url"],
            )
    else:
        logger.warning(
            "Connected to LOCAL SQLite: %s — this is not the Turso cloud database",
            target["url"],
        )
    return _client


async def probe() -> bool:
    """Execute one real query, so a fresh deployment shows a read on the dashboard.

    Raises ``DatabaseError`` when the database cannot be reached; the caller
    decides whether that is fatal.
    """
    try:
        async with asyncio.timeout(get_settings().db_timeout_s):
            result = await get_client().execute("SELECT 1 AS ok")
        rows = getattr(result, "rows", ()) or ()
        return bool(rows)
    except DatabaseError:
        raise
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise DatabaseError("database probe timed out") from exc
    except Exception as exc:
        raise DatabaseError(f"database probe failed: {exc}") from exc


async def close() -> None:
    global _client
    if _client is None:
        return
    try:
        await _client.close()
    except Exception as exc:  # never let teardown raise out of lifespan
        logger.warning("database close failed: %s", exc)
    finally:
        _client = None


def get_client() -> libsql_client.Client:
    if _client is None:
        raise DatabaseError("database not initialised")
    return _client


async def _execute(
    sql: str,
    params: Sequence[Any] | None = None,
    *,
    timeout_s: float | None = None,
) -> Any:
    client = get_client()
    cfg = get_settings()
    budget = timeout_s if timeout_s is not None else cfg.db_timeout_s
    try:
        async with asyncio.timeout(budget):
            return await client.execute(sql, list(params or []))
    except DatabaseError:
        raise
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise DatabaseError(f"database query timed out after {budget}s") from exc
    except Exception as exc:
        raise DatabaseError(f"database query failed: {exc}") from exc


async def _migrate_cache_table() -> None:
    """Rebuild ``search_records`` if it predates the per-query cache key.

    The old table had ``url UNIQUE``, which let one query steal another query's
    rows. It is a cache, so the rebuild drops the rows rather than migrating
    them — which also clears any results poisoned by the old relevance bug.
    """
    client = get_client()
    try:
        info = await client.execute("PRAGMA table_info(search_records)")
        columns = {str(row[1]) for row in (getattr(info, "rows", ()) or ())}
    except Exception as exc:
        logger.debug("cache schema probe skipped: %s", exc)
        return

    if not columns or "query_key" in columns:
        return

    logger.warning("rebuilding search_records: the old schema had no per-query cache key")
    try:
        await client.execute("DROP TABLE IF EXISTS search_records")
    except Exception as exc:
        raise DatabaseError(f"cache table migration failed: {exc}") from exc


async def initialize_schema() -> None:
    """Apply the schema. Idempotent, so it is safe on every cold start."""
    client = get_client()
    cfg = get_settings()
    await _migrate_cache_table()
    try:
        async with asyncio.timeout(cfg.db_timeout_s * 3):
            await client.batch(list(SCHEMA_STATEMENTS) + list(ENGINE_SCHEMA))
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise DatabaseError("schema initialisation timed out") from exc
    except Exception as exc:
        raise DatabaseError(f"schema initialisation failed: {exc}") from exc
    await purge_unverified_cache()
    logger.info("schema ready on %s", describe_target()["kind"])


async def purge_unverified_cache() -> int:
    """Empty `search_records`, which held raw provider dumps.

    Nothing writes to that table any more: verified pages live in `documents`
    and `documents_fts`. The purge runs at startup and is a no-op once the table
    is empty, so old unverified rows can never surface again.
    """
    try:
        result = await _execute("DELETE FROM search_records")
        removed = int(getattr(result, "rows_affected", 0) or 0)
        if removed:
            logger.warning("purged %d unverified cached rows from search_records", removed)
        return removed
    except Exception as exc:
        logger.warning("cache purge skipped: %s", exc)
        return 0


async def ping() -> bool:
    """Cheap liveness probe used by /health. Never raises."""
    try:
        await _execute("SELECT 1", timeout_s=2.0)
        return True
    except Exception as exc:
        logger.warning("database ping failed: %s", exc)
        return False


async def fetch_cached(
    query: str,
    limit: int,
    *,
    ttl_hours: int | None = None,
) -> list[dict[str, Any]]:
    """Return cached records for exactly this query, newer than the TTL.

    The lookup is by ``query_cache_key(query)`` — never by a shared or partial
    key — so a query can only ever see rows written for itself.
    """
    cfg = get_settings()
    ttl = cfg.cache_ttl_hours if ttl_hours is None else ttl_hours
    result = await _execute(_SELECT_CACHED_SQL, [query_cache_key(query), _cutoff(ttl), int(limit)])
    return _rows_to_dicts(result)


async def count_cached(query: str, *, ttl_hours: int | None = None) -> int:
    cfg = get_settings()
    ttl = cfg.cache_ttl_hours if ttl_hours is None else ttl_hours
    result = await _execute(_COUNT_SQL, [query_cache_key(query), _cutoff(ttl)])
    rows = _rows_to_dicts(result)
    if not rows:
        return 0
    try:
        return int(rows[0].get("n") or 0)
    except (TypeError, ValueError):
        return 0


async def upsert_records(query: str, records: Sequence[dict[str, Any]]) -> int:
    """Persist retrieval results in a single batched round trip.

    Rows are keyed by ``(query_cache_key(query), url)``: the same URL can be
    cached under several queries without one query taking it away from another,
    and a query can never be served rows that belong to a different one.
    """
    if not records:
        return 0
    key = query_cache_key(query)
    now = _timestamp()
    statements: list[tuple[str, list[Any]]] = []
    for record in records:
        url = (record.get("url") or "").strip()
        if not url:
            continue
        statements.append(
            (
                _UPSERT_SQL,
                [
                    key,
                    normalise_query(query)[:200],
                    (record.get("title") or url)[:500],
                    url[:2000],
                    (record.get("snippet") or "")[:2000],
                    record.get("poster_url"),
                    (record.get("source") or "live_crawl")[:64],
                    now,
                ],
            )
        )
    if not statements:
        return 0
    client = get_client()
    cfg = get_settings()
    try:
        async with asyncio.timeout(cfg.db_timeout_s * 2):
            await client.batch(statements)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise DatabaseError("cache write timed out") from exc
    except Exception as exc:
        raise DatabaseError(f"cache write failed: {exc}") from exc
    return len(statements)


async def purge_expired(*, ttl_hours: int | None = None) -> int:
    """Drop rows older than the TTL so the free-tier database stays small."""
    cfg = get_settings()
    ttl = cfg.cache_ttl_hours if ttl_hours is None else ttl_hours
    # Keep a generous window: 7x TTL, so a popular query stays warm.
    result = await _execute(_PURGE_SQL, [_cutoff(ttl * 7)])
    rows = getattr(result, "rows", None)
    try:
        return int(getattr(result, "rows_affected", 0) or (len(rows) if rows else 0))
    except (TypeError, ValueError):
        return 0
