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
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

import libsql_client

from app.config import Settings, get_settings
from app.engine.schema import (
    ENGINE_MIGRATIONS,
    ENGINE_SCHEMA,
    LEGACY_TABLES,
    LEGACY_TRIGGERS,
    PAGE_FTS,
    PAGE_TRIGGERS,
)
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
# Base schema. The result cache and the flat `documents` model are gone: the
# two-tier index in app/engine/schema.py is the only store now.
SCHEMA_STATEMENTS: tuple[str, ...] = ()

_client: libsql_client.Client | None = None


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


@dataclass(slots=True)
class SimpleResult:
    """The shape callers rely on when a driver returns something unexpected."""

    rows: tuple[Any, ...] = ()
    columns: tuple[str, ...] = ()


def _normalise_result(result: Any) -> Any:
    """Coerce a raw driver payload into an object with `.rows`/`.columns`.

    LibSQL has returned `{"result": {...}}` in some builds and bare
    `{"rows": ...}` in others; a response with neither key used to raise
    `KeyError: 'result'` deep inside the driver. Anything unrecognised becomes
    an empty result rather than an exception.
    """
    if result is None:
        return SimpleResult()
    if hasattr(result, "rows"):
        return result
    if isinstance(result, dict):
        payload = result.get("result", result)
        if isinstance(payload, dict):
            rows = payload.get("rows") or payload.get("results") or ()
            columns = payload.get("columns") or ()
            return SimpleResult(rows=tuple(rows), columns=tuple(columns))
        if isinstance(payload, (list, tuple)):
            return SimpleResult(rows=tuple(payload))
    return SimpleResult()


async def execute_safe(
    sql: str,
    params: Sequence[Any] | None = None,
    *,
    timeout_s: float | None = None,
    default: Any | None = None,
) -> Any:
    """Execute a statement and never surface a driver parsing KeyError.

    A response missing its `result` key means "no rows" for our purposes: every
    caller here either reads rows or ignores the return value. Real failures
    (timeouts, network) still raise `DatabaseError`.
    """
    for attempt in (1, 2):
        try:
            return _normalise_result(await _execute(sql, params, timeout_s=timeout_s))
        except DatabaseError as exc:
            cause = exc.__cause__
            parse_hiccup = isinstance(cause, KeyError) or "'result'" in str(exc)
            if not parse_hiccup:
                raise
            if attempt == 1:
                # A malformed response is usually transient: try once more before
                # deciding the statement simply produced no rows.
                await asyncio.sleep(0.05)
                continue
            logger.warning(
                "libsql response without 'result' for %r — treating as no rows (%s)",
                sql.strip().split("\n")[0][:70],
                exc,
            )
            return default if default is not None else SimpleResult()
    return default if default is not None else SimpleResult()


async def _drop_legacy_index() -> None:
    """Remove the flat `documents` model and the legacy result cache.

    Both were derived data from earlier releases; the two-tier `sites`/`pages`
    schema replaces them, and the brief asks for a clean recreation.
    """
    client = get_client()
    statements = [f"DROP TRIGGER IF EXISTS {name}" for name in LEGACY_TRIGGERS]
    statements += [f"DROP TABLE IF EXISTS {name}" for name in LEGACY_TABLES]
    try:
        await client.batch(statements)
    except Exception as exc:
        raise DatabaseError(f"legacy index cleanup failed: {exc}") from exc


async def _apply_migrations() -> None:
    """Add columns that arrived after the first release (safe to repeat)."""
    client = get_client()
    for table, column_ddl in ENGINE_MIGRATIONS:
        try:
            await client.execute(f"ALTER TABLE {table} ADD COLUMN {column_ddl}")
            logger.info("migration applied: %s.%s", table, column_ddl.split()[0])
        except Exception:
            continue  # already present


async def _ensure_page_fts() -> None:
    """Create or repair the external-content FTS index and its triggers."""
    client = get_client()
    try:
        info = await client.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='page_fts'"
        )
        rows = getattr(info, "rows", ()) or ()
        existing_sql = str(rows[0][0]) if rows else ""
    except Exception as exc:
        logger.debug("page_fts probe skipped: %s", exc)
        existing_sql = ""

    needs_rebuild = (
        not existing_sql
        or "content='pages'" not in existing_sql
        or "unicode61" not in existing_sql
    )

    if needs_rebuild:
        logger.info("building page_fts as external-content FTS5 (unicode61)")
        try:
            await client.batch(
                [
                    "DROP TRIGGER IF EXISTS pages_ai",
                    "DROP TRIGGER IF EXISTS pages_ad",
                    "DROP TRIGGER IF EXISTS pages_au",
                    "DROP TABLE IF EXISTS page_fts",
                    PAGE_FTS,
                    *PAGE_TRIGGERS,
                ]
            )
            await client.execute("INSERT INTO page_fts(page_fts) VALUES('rebuild')")
        except Exception as exc:
            raise DatabaseError(f"page_fts rebuild failed: {exc}") from exc
        return

    try:
        await client.batch(list(PAGE_TRIGGERS))
    except Exception as exc:
        logger.warning("could not ensure page triggers: %s", exc)


async def initialize_schema() -> None:
    """Apply the schema. Idempotent, so it is safe on every cold start."""
    client = get_client()
    cfg = get_settings()
    await _drop_legacy_index()
    try:
        async with asyncio.timeout(cfg.db_timeout_s * 3):
            await client.batch(list(SCHEMA_STATEMENTS) + list(ENGINE_SCHEMA))
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise DatabaseError("schema initialisation timed out") from exc
    except Exception as exc:
        raise DatabaseError(f"schema initialisation failed: {exc}") from exc
    # Runs after the base tables exist: the FTS rebuild reads `pages`.
    await _ensure_page_fts()
    await _apply_migrations()
    try:
        counts = await get_client().execute(
            "SELECT (SELECT COUNT(*) FROM sites), (SELECT COUNT(*) FROM pages), "
            "(SELECT COUNT(*) FROM page_fts)"
        )
        rows = getattr(counts, "rows", ()) or ()
        sites, pages, indexed = (int(rows[0][0]), int(rows[0][1]), int(rows[0][2])) if rows else (0, 0, 0)
        logger.info(
            "schema ready on %s: %d sites, %d pages, %d indexed",
            describe_target()["kind"],
            sites,
            pages,
            indexed,
        )
    except Exception as exc:  # pragma: no cover - observability only
        logger.info("schema ready on %s (%s)", describe_target()["kind"], exc)


async def ping() -> bool:
    """Cheap liveness probe used by /health. Never raises."""
    try:
        await _execute("SELECT 1", timeout_s=2.0)
        return True
    except Exception as exc:
        logger.warning("database ping failed: %s", exc)
        return False


