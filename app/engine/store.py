"""Storage for the proprietary index: documents, FTS5 search, backlog, queue.

All access goes through the gateway's single shared libsql client, so the engine
adds no connections and no new failure modes. Queries are parameterised; FTS
input is sanitised because MATCH treats punctuation as syntax.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from app.database import get_client
from app.engine.config import EngineSettings, get_engine_settings
from app.engine.ranking import RankedDocument, rank_documents
from app.engine.schema import ENGINE_SCHEMA
from app.errors import DatabaseError

logger = logging.getLogger(__name__)

_SAFE_TOKEN = re.compile(r"[A-Za-z0-9\u0900-\u097F\u0C80-\u0CFF]+")

_UPSERT_DOCUMENT = """
INSERT INTO documents (
    url, canonical_url, domain, title, snippet, content, language, favicon_url, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(url) DO UPDATE SET
    canonical_url = excluded.canonical_url,
    domain = excluded.domain,
    title = excluded.title,
    snippet = excluded.snippet,
    content = excluded.content,
    language = excluded.language,
    favicon_url = excluded.favicon_url,
    updated_at = excluded.updated_at
"""

_DELETE_FTS = "DELETE FROM documents_fts WHERE url = ?"
_INSERT_FTS = """
INSERT INTO documents_fts (url, title, snippet, content, language, updated_at)
VALUES (?, ?, ?, ?, ?, ?)
"""

_SEARCH_FTS = """
SELECT url, title, snippet, language, updated_at, bm25(documents_fts) AS rank
FROM documents_fts
WHERE documents_fts MATCH ?
ORDER BY rank
LIMIT ?
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def build_match_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Tokens are quoted (so punctuation cannot become syntax) and joined with an
    implicit AND; the last token gets a prefix wildcard so "bengal" finds
    "bengaluru" while the user is still typing.
    """
    tokens = _SAFE_TOKEN.findall(query or "")
    if not tokens:
        return ""
    quoted = [f'"{token}"' for token in tokens[:-1]]
    quoted.append(f'"{tokens[-1]}"*')
    return " ".join(quoted)


class IndexStore:
    """Typed access to the index tables."""

    def __init__(self, settings: EngineSettings | None = None) -> None:
        self.settings = settings or get_engine_settings()

    async def ensure_schema(self) -> None:
        client = get_client()
        try:
            await client.batch(list(ENGINE_SCHEMA))
        except Exception as exc:
            raise DatabaseError(f"index schema initialisation failed: {exc}") from exc

    # -- documents ------------------------------------------------------
    async def upsert_documents(self, documents: Sequence[dict[str, Any]]) -> int:
        if not documents:
            return 0
        client = get_client()
        stamp = _now()
        statements: list[tuple[str, list[Any]]] = []
        for doc in documents:
            url = str(doc.get("url") or "").strip()
            title = str(doc.get("title") or "").strip()
            if not url or not title:
                continue
            content = str(doc.get("content") or "")
            statements.append(
                (
                    _UPSERT_DOCUMENT,
                    [
                        url,
                        doc.get("canonical_url") or url,
                        doc.get("domain") or "",
                        title,
                        doc.get("snippet") or "",
                        content,
                        doc.get("language") or "en",
                        doc.get("favicon_url"),
                        stamp,
                    ],
                )
            )
            statements.append((_DELETE_FTS, [url]))
            statements.append(
                (
                    _INSERT_FTS,
                    [url, title, doc.get("snippet") or "", content, doc.get("language") or "en", stamp],
                )
            )

        if not statements:
            return 0
        try:
            await client.batch(statements)
        except Exception as exc:
            raise DatabaseError(f"index write failed: {exc}") from exc
        return len(documents)

    # -- search ---------------------------------------------------------
    async def search_local(self, query: str, limit: int | None = None) -> list[RankedDocument]:
        """Query the FTS5 index. Returns ranked documents, best first."""
        match = build_match_query(query)
        if not match:
            return []

        client = get_client()
        capped = limit or self.settings.index_limit
        try:
            result = await client.execute(_SEARCH_FTS, [match, capped])
        except Exception as exc:
            # A malformed MATCH is a miss, not a 500: fall back to the providers.
            logger.warning("index query failed for %r: %s", query, exc)
            return []

        columns = tuple(getattr(result, "columns", ()) or ())
        rows: Iterable[Any] = getattr(result, "rows", ()) or ()
        documents: list[RankedDocument] = []
        for row in rows:
            record = {name: row[idx] for idx, name in enumerate(columns)} if columns else {}
            documents.append(
                RankedDocument(
                    url=str(record.get("url") or ""),
                    title=str(record.get("title") or ""),
                    snippet=str(record.get("snippet") or ""),
                    language=str(record.get("language") or "en"),
                    updated_at=str(record.get("updated_at") or "") or None,
                    bm25=float(record.get("rank") or 0.0),
                )
            )
        return rank_documents(documents, freshness_weight=self.settings.freshness_weight)

    async def count_documents(self) -> int:
        client = get_client()
        result = await client.execute("SELECT COUNT(*) AS n FROM documents")
        rows = getattr(result, "rows", ()) or ()
        return int(rows[0][0]) if rows else 0

    # -- crawler backlog ------------------------------------------------
    async def enqueue_backlog(self, query: str, hits: int) -> None:
        """Record a query the index could not answer, so the crawler can target it."""
        cleaned = (query or "").strip().lower()
        if not cleaned:
            return
        client = get_client()
        try:
            await client.execute(
                """
                INSERT INTO crawl_backlog (query, hits, status, created_at)
                VALUES (?, ?, 'pending', ?)
                ON CONFLICT(query) DO UPDATE SET
                    hits = crawl_backlog.hits + 1,
                    status = 'pending'
                """,
                [cleaned, max(0, hits), _now()],
            )
        except Exception as exc:
            logger.warning("backlog enqueue failed for %r: %s", query, exc)

    async def pending_backlog(self, limit: int = 20) -> list[str]:
        client = get_client()
        result = await client.execute(
            "SELECT query FROM crawl_backlog WHERE status = 'pending' ORDER BY hits DESC, created_at ASC LIMIT ?",
            [limit],
        )
        rows = getattr(result, "rows", ()) or ()
        return [str(row[0]) for row in rows]

    async def mark_backlog_processed(self, query: str) -> None:
        client = get_client()
        await client.execute(
            "UPDATE crawl_backlog SET status = 'processed', processed_at = ? WHERE query = ?",
            [_now(), query.strip().lower()],
        )

    async def enqueue_urls(self, urls: Iterable[str], reason: str) -> int:
        client = get_client()
        statements: list[tuple[str, list[Any]]] = []
        for url in urls:
            statements.append(
                (
                    """
                    INSERT INTO crawl_queue (url, domain, depth, reason, status, created_at)
                    VALUES (?, ?, 0, ?, 'pending', ?)
                    ON CONFLICT(url) DO NOTHING
                    """,
                    [url, url.split("/")[2] if "://" in url and len(url.split("/")) > 2 else "", reason, _now()],
                )
            )
        if not statements:
            return 0
        await client.batch(statements)
        return len(statements)

    async def stats(self) -> dict[str, Any]:
        """Index health, surfaced on /health."""
        try:
            documents = await self.count_documents()
            client = get_client()
            backlog = await client.execute("SELECT COUNT(*) FROM crawl_backlog WHERE status = 'pending'")
            rows = getattr(backlog, "rows", ()) or ()
            pending = int(rows[0][0]) if rows else 0
            return {"documents": documents, "backlog": pending}
        except Exception as exc:
            logger.warning("index stats unavailable: %s", exc)
            return {"documents": 0, "backlog": 0}


_store: IndexStore | None = None


def get_store(settings: EngineSettings | None = None) -> IndexStore:
    global _store
    if _store is None:
        _store = IndexStore(settings)
    return _store


__all__ = ["IndexStore", "get_store", "build_match_query"]
