"""Storage for the proprietary index: bounded documents, FTS5, backlog, queue.

Policy lives here, not in the crawler: ingestion rejects duplicates and
over-cap domains, revalidation updates in place, and the request path only ever
reads. All access goes through the gateway's single shared libsql client.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from app.database import get_client
from app.engine.config import EngineSettings, get_engine_settings
from app.engine.ranking import RankedDocument, rank_documents
from app.engine.schema import ENGINE_MIGRATIONS, ENGINE_SCHEMA
from app.engine.urls import domain_class, is_indexable, normalise_url, root_domain
from app.errors import DatabaseError

logger = logging.getLogger(__name__)

_SAFE_TOKEN = re.compile(r"[A-Za-z0-9\u0900-\u097F\u0C80-\u0CFF]+")

_INSERT_DOCUMENT = """
INSERT INTO documents (
    url, canonical_url, domain, root_domain, domain_class, title, snippet, content,
    language, favicon_url, payload_bytes, content_hash, etag, last_modified,
    last_checked_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_DOCUMENT = """
UPDATE documents SET
    canonical_url = ?, domain = ?, root_domain = ?, domain_class = ?, title = ?,
    snippet = ?, content = ?, language = ?, favicon_url = ?, payload_bytes = ?,
    content_hash = ?, etag = ?, last_modified = ?, last_checked_at = ?, updated_at = ?
WHERE url = ?
"""

_DELETE_FTS = "DELETE FROM documents_fts WHERE url = ?"
_INSERT_FTS = """
INSERT INTO documents_fts (url, title, snippet, content, language, updated_at)
VALUES (?, ?, ?, ?, ?, ?)
"""

_SEARCH_FTS = """
SELECT f.url, f.title, f.snippet, f.language, f.updated_at, bm25(documents_fts) AS rank,
       d.favicon_url AS favicon
FROM documents_fts f
LEFT JOIN documents d ON d.url = f.url
WHERE documents_fts MATCH ?
ORDER BY rank
LIMIT ?
"""

# Autocomplete: the document titles that match, most relevant first.
_SUGGEST_FTS = """
SELECT f.title
FROM documents_fts f
WHERE documents_fts MATCH ?
ORDER BY bm25(documents_fts)
LIMIT ?
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _content_hash(title: str, snippet: str) -> str:
    digest = hashlib.sha256(f"{title}\n{snippet}".encode("utf-8")).hexdigest()
    return digest[:32]


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
    """Typed access to the index tables, with the storage policy enforced."""

    def __init__(self, settings: EngineSettings | None = None) -> None:
        self.settings = settings or get_engine_settings()

    async def ensure_schema(self) -> None:
        client = get_client()
        try:
            await client.batch(list(ENGINE_SCHEMA))
        except Exception as exc:
            raise DatabaseError(f"index schema initialisation failed: {exc}") from exc
        await self._apply_migrations()

    async def _apply_migrations(self) -> None:
        """Add columns introduced after the first release (idempotent)."""
        client = get_client()
        for table, column_definition in ENGINE_MIGRATIONS:
            try:
                await client.execute(f"ALTER TABLE {table} ADD COLUMN {column_definition}")
                logger.info("index migration applied: %s.%s", table, column_definition.split()[0])
            except Exception:
                # Already present — the only expected failure.
                continue

    # -- ingestion ------------------------------------------------------
    async def ingest_documents(self, documents: Sequence[dict[str, Any]]) -> dict[str, int]:
        """Insert new documents; reject duplicates, over-cap domains and oversize rows."""
        outcome = {"inserted": 0, "duplicate": 0, "domain_cap": 0, "not_indexable": 0, "over_budget": 0}
        if not documents:
            return outcome

        client = get_client()
        stamp = _now()

        counts: dict[str, int] = {}
        try:
            existing = await client.execute("SELECT root_domain, COUNT(*) FROM documents GROUP BY root_domain")
            for row in getattr(existing, "rows", ()) or ():
                counts[str(row[0] or "")] = int(row[1])
        except Exception as exc:
            logger.warning("domain counts unavailable: %s", exc)

        statements: list[tuple[str, list[Any]]] = []
        for doc in documents:
            url = normalise_url(str(doc.get("url") or ""))
            title = str(doc.get("title") or "").strip()
            if not url or not title:
                outcome["not_indexable"] += 1
                continue
            if not is_indexable(url):
                outcome["not_indexable"] += 1
                continue

            payload = int(doc.get("payload_bytes") or 0)
            if payload > self.settings.max_payload_bytes:
                outcome["over_budget"] += 1
                continue

            domain = root_domain(url)
            if counts.get(domain, 0) >= self.settings.max_urls_per_domain:
                outcome["domain_cap"] += 1
                continue

            # Reject, do not overwrite: revalidation is the only path that updates.
            try:
                probe = await client.execute("SELECT 1 FROM documents WHERE url = ? LIMIT 1", [url])
                if getattr(probe, "rows", ()):
                    outcome["duplicate"] += 1
                    continue
            except Exception as exc:
                logger.warning("duplicate probe failed for %s: %s", url, exc)

            content = str(doc.get("content") or "")
            statements.append(
                (
                    _INSERT_DOCUMENT,
                    [
                        url,
                        doc.get("canonical_url") or url,
                        doc.get("domain") or "",
                        domain,
                        doc.get("domain_class") or domain_class(url),
                        title,
                        doc.get("snippet") or "",
                        content,
                        doc.get("language") or "en",
                        doc.get("favicon_url"),
                        payload,
                        _content_hash(title, str(doc.get("snippet") or "")),
                        None,
                        None,
                        None,   # last_checked_at: a crawl is not a revalidation
                        stamp,
                    ],
                )
            )
            statements.append((_DELETE_FTS, [url]))
            statements.append(
                (_INSERT_FTS, [url, title, doc.get("snippet") or "", content, doc.get("language") or "en", stamp])
            )
            counts[domain] = counts.get(domain, 0) + 1
            outcome["inserted"] += 1

        if statements:
            try:
                await client.batch(statements)
            except Exception as exc:
                raise DatabaseError(f"index write failed: {exc}") from exc
        return outcome

    async def update_document(
        self,
        url: str,
        document: dict[str, Any],
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> bool:
        """Update an existing row in place. Returns False when there is no such row."""
        key = normalise_url(url)
        client = get_client()
        stamp = _now()
        title = str(document.get("title") or "").strip()
        snippet = str(document.get("snippet") or "")
        content = str(document.get("content") or "")

        probe = await client.execute("SELECT 1 FROM documents WHERE url = ? LIMIT 1", [key])
        if not getattr(probe, "rows", ()):
            return False

        try:
            await client.batch(
                [
                    (
                        _UPDATE_DOCUMENT,
                        [
                            document.get("canonical_url") or key,
                            document.get("domain") or "",
                            root_domain(key),
                            document.get("domain_class") or domain_class(key),
                            title,
                            snippet,
                            content,
                            document.get("language") or "en",
                            document.get("favicon_url"),
                            int(document.get("payload_bytes") or 0),
                            _content_hash(title, snippet),
                            etag,
                            last_modified,
                            stamp,
                            stamp,
                            key,
                        ],
                    ),
                    (_DELETE_FTS, [key]),
                    (_INSERT_FTS, [key, title, snippet, content, document.get("language") or "en", stamp]),
                ]
            )
        except Exception as exc:
            raise DatabaseError(f"index update failed: {exc}") from exc
        return True

    async def get_document(self, url: str) -> dict[str, Any] | None:
        client = get_client()
        result = await client.execute("SELECT * FROM documents WHERE url = ? LIMIT 1", [normalise_url(url)])
        columns = tuple(getattr(result, "columns", ()) or ())
        rows = getattr(result, "rows", ()) or ()
        if not rows:
            return None
        return {name: rows[0][idx] for idx, name in enumerate(columns)}

    # -- revalidation ---------------------------------------------------
    async def stale_documents(self, limit: int = 10) -> list[dict[str, Any]]:
        """Documents whose class TTL has expired, stalest first."""
        client = get_client()
        fetch_limit = max(limit * 5, 25)
        result = await client.execute(
            """
            SELECT url, etag, last_modified, domain_class, last_checked_at, updated_at
            FROM documents
            ORDER BY COALESCE(last_checked_at, '1970-01-01') ASC
            LIMIT ?
            """,
            [fetch_limit],
        )
        columns = tuple(getattr(result, "columns", ()) or ())
        rows = getattr(result, "rows", ()) or ()

        now = datetime.now(timezone.utc)
        ttls = {
            "news": timedelta(days=self.settings.news_ttl_days),
            "government": timedelta(days=self.settings.government_ttl_days),
            "static": timedelta(days=self.settings.static_ttl_days),
        }
        stale: list[dict[str, Any]] = []
        for row in rows:
            record = {name: row[idx] for idx, name in enumerate(columns)}
            # Never revalidated ⇒ stale by definition; otherwise compare against
            # the TTL for the document's domain class.
            checked = self._parse(record.get("last_checked_at"))
            ttl = ttls.get(str(record.get("domain_class") or "static"), ttls["static"])
            if checked is None or now - checked >= ttl:
                stale.append(record)
            if len(stale) >= limit:
                break
        return stale

    @staticmethod
    def _parse(value: Any) -> datetime | None:
        if not value:
            return None
        text = str(value)[:19]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[: len(fmt) + 2].strip(), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    # -- search ---------------------------------------------------------
    async def search_local(self, query: str, limit: int | None = None) -> list[RankedDocument]:
        """Query the FTS5 index. The only read the request path performs."""
        match = build_match_query(query)
        if not match:
            return []

        client = get_client()
        capped = limit or self.settings.index_limit
        try:
            result = await client.execute(_SEARCH_FTS, [match, capped])
        except Exception as exc:
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
                    favicon_url=str(record.get("favicon") or "") or None,
                )
            )
        return rank_documents(documents, freshness_weight=self.settings.freshness_weight)

    async def suggest(self, query: str, limit: int = 8) -> list[str]:
        """OpenSearch autocomplete: matching document titles, deduplicated.

        Falls back to a prefix match when the strict MATCH finds nothing, so a
        half-typed word still returns something useful.
        """
        tokens = _SAFE_TOKEN.findall(query or "")
        if not tokens:
            return []

        client = get_client()
        attempts = [build_match_query(query), f'"{tokens[-1]}"*' if tokens else ""]
        seen: set[str] = set()
        suggestions: list[str] = []

        for match in attempts:
            if not match:
                continue
            try:
                result = await client.execute(_SUGGEST_FTS, [match, max(limit * 2, 12)])
            except Exception as exc:
                logger.warning("suggest query failed for %r: %s", query, exc)
                continue
            for row in getattr(result, "rows", ()) or ():
                title = str(row[0] or "").strip()
                key = title.lower()
                if not title or key in seen:
                    continue
                seen.add(key)
                suggestions.append(title[:80])
                if len(suggestions) >= limit:
                    return suggestions
        return suggestions

    async def count_documents(self) -> int:
        client = get_client()
        result = await client.execute("SELECT COUNT(*) AS n FROM documents")
        rows = getattr(result, "rows", ()) or ()
        return int(rows[0][0]) if rows else 0

    async def max_payload_bytes(self) -> int:
        client = get_client()
        result = await client.execute("SELECT COALESCE(MAX(payload_bytes), 0) FROM documents")
        rows = getattr(result, "rows", ()) or ()
        return int(rows[0][0]) if rows else 0

    async def domain_counts(self) -> dict[str, int]:
        client = get_client()
        result = await client.execute("SELECT root_domain, COUNT(*) FROM documents GROUP BY root_domain")
        return {str(row[0] or ""): int(row[1]) for row in getattr(result, "rows", ()) or ()}

    # -- crawler backlog ------------------------------------------------
    async def enqueue_backlog(self, query: str, hits: int) -> None:
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
        return [str(row[0]) for row in getattr(result, "rows", ()) or ()]

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
            normalised = normalise_url(url)
            if not normalised or not is_indexable(normalised):
                continue
            statements.append(
                (
                    """
                    INSERT INTO crawl_queue (url, domain, depth, reason, status, created_at)
                    VALUES (?, ?, 0, ?, 'pending', ?)
                    ON CONFLICT(url) DO NOTHING
                    """,
                    [normalised, root_domain(normalised), reason, _now()],
                )
            )
        if not statements:
            return 0
        await client.batch(statements)
        return len(statements)

    async def stats(self) -> dict[str, int]:
        """Index health, surfaced on /health."""
        try:
            documents = await self.count_documents()
            client = get_client()
            backlog = await client.execute("SELECT COUNT(*) FROM crawl_backlog WHERE status = 'pending'")
            rows = getattr(backlog, "rows", ()) or ()
            pending = int(rows[0][0]) if rows else 0
            return {
                "documents": documents,
                "backlog": pending,
                "max_payload_bytes": await self.max_payload_bytes(),
            }
        except Exception as exc:
            logger.warning("index stats unavailable: %s", exc)
            return {"documents": 0, "backlog": 0, "max_payload_bytes": 0}


_store: IndexStore | None = None


def get_store(settings: EngineSettings | None = None) -> IndexStore:
    global _store
    if _store is None:
        _store = IndexStore(settings)
    return _store


__all__ = ["IndexStore", "get_store", "build_match_query"]
