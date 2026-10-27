"""The index store: every read and write of the page index.

Two rules shape this module.

**Persistence is one trip and it is atomic.** A page write and the matching
`crawl_queue` transition go into a single `BEGIN IMMEDIATE … COMMIT` batch. If
any statement in that batch fails, none of it lands and the URL is not marked
processed — which is the difference between "the crawl made progress" and "the
queue drained into nothing".

**Counts are proved, not assumed.** `persist_batch` reads `COUNT(*) FROM pages`
inside the same transaction that wrote the rows, so the number it reports is a
number the database confirmed, not a number the caller hoped for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.engine.config import Settings, get_settings
from app.libsql_http import LibsqlClient, LibsqlError

logger = logging.getLogger(__name__)

# Columns the FTS index carries, in bm25 weight order.
BM25_WEIGHTS = (10.0, 5.0, 1.0)


@dataclass
class PageDocument:
    """One page, ready to be stored."""

    url: str
    domain: str
    title: str
    description: str
    body_text: str
    quality_score: float = 1.0
    digest: str = ""

    def as_row(self) -> tuple[Any, ...]:
        """The seven bound values the page insert expects, in SQL order.

        `quality_score` is cast to `float` here rather than trusted from the
        caller: it is a REAL column, and an `int` reaching the encoder would be
        sent as an integer while a `Decimal` would be sent as text. Turso
        rejects both for a REAL column.
        """
        return (
            self.domain,
            self.url,
            self.title,
            self.description,
            self.body_text,
            float(self.quality_score),
            self.digest,
        )


@dataclass
class QueuedUrl:
    """A row of `crawl_queue` handed to the pipeline."""

    rowid: int
    url: str
    domain: str
    retry_count: int = 0


@dataclass
class PersistOutcome:
    """What one persistence batch actually achieved."""

    pages_written: int = 0
    total_pages: int = 0
    sites_written: int = 0
    queue_closed: int = 0
    duplicates: int = 0
    committed: bool = False
    reason: str = ""


@dataclass
class SearchHit:
    """One row of an index search."""

    page_id: int
    url: str
    title: str
    description: str
    body_text: str
    score: float = 0.0

    @property
    def snippet(self) -> str:
        text = (self.description or "").strip()
        if text:
            return text
        body = " ".join((self.body_text or "").split())
        return body[:200] + ("…" if len(body) > 200 else "")


@dataclass
class Stats:
    sites: int = 0
    pages: int = 0
    indexed: int = 0
    queued: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sites": self.sites,
            "pages": self.pages,
            "indexed": self.indexed,
            "queued": self.queued,
            **self.extra,
        }


# ---------------------------------------------------------------------------
# FTS query building
# ---------------------------------------------------------------------------

_FTS_UNSAFE = set('"\':*()[]{}^~+-')


def fts_tokens(query: str) -> list[str]:
    """Split a user query into FTS-safe terms.

    FTS5 treats `-`, `:`, `*`, quotes and brackets as syntax, so a query of
    "covid-19: what now?" is a syntax error unless every term is quoted. Each
    token is therefore reduced to word characters and emitted as a quoted
    string.
    """
    tokens: list[str] = []
    for raw in (query or "").lower().split():
        cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in "_/.")
        cleaned = cleaned.strip("_/.")
        if cleaned:
            tokens.append(cleaned)
    return tokens


def build_match_query(query: str, *, prefix: bool = False) -> str:
    """Turn a user query into an FTS5 MATCH expression."""
    tokens = fts_tokens(query)
    if not tokens:
        return ""
    if prefix:
        return " OR ".join(f'"{token}"*' for token in tokens)
    return " AND ".join(f'"{token}"' for token in tokens)


def normalise_score(raw: float) -> float:
    """bm25 is lower-is-better and unbounded; map it to 0..1 for the API."""
    try:
        value = abs(float(raw))
    except (TypeError, ValueError):
        return 0.0
    return round(1.0 / (1.0 + value), 4)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class IndexStore:
    """All index reads and writes, over one injected database client."""

    def __init__(self, client: LibsqlClient, settings: Settings | None = None) -> None:
        self._client = client
        self._settings = settings or get_settings()

    @property
    def client(self) -> LibsqlClient:
        return self._client

    async def ensure_schema(self) -> dict[str, int]:
        from app.database import initialize_database

        return await initialize_database(self._client)

    # -- writes ------------------------------------------------------------

    async def persist_batch(
        self,
        documents: Sequence[PageDocument],
        queue_ids: Sequence[int] = (),
    ) -> PersistOutcome:
        """Write pages and close their queue rows in one atomic batch."""
        if not documents and not queue_ids:
            return PersistOutcome(committed=True, reason="nothing to write")

        statements: list[tuple[str, Sequence[Any]]] = [("BEGIN IMMEDIATE", ())]

        for document in documents:
            statements.append(
                (
                    "INSERT INTO sites (domain, canonical_url) VALUES (?, ?) "
                    "ON CONFLICT(domain) DO NOTHING",
                    (document.domain, f"https://{document.domain}/"),
                )
            )

        for document in documents:
            statements.append(
                (
                    "INSERT OR IGNORE INTO pages "
                    "(site_id, url, title, description, body_text, quality_score, digest, indexed_at) "
                    "VALUES ((SELECT site_id FROM sites WHERE domain = ?), ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    document.as_row(),
                )
            )

        for rowid in queue_ids:
            statements.append(
                (
                    "UPDATE crawl_queue SET status = 'processed', "
                    "processed_at = CURRENT_TIMESTAMP WHERE rowid = ?",
                    (rowid,),
                )
            )

        statements.append(("SELECT COUNT(*) FROM pages", ()))
        statements.append(("SELECT COUNT(*) FROM sites", ()))
        statements.append(("COMMIT", ()))

        try:
            results = await self._client.batch(statements)
        except LibsqlError as error:
            await self._rollback_quietly()
            logger.error("persistence batch failed: %s", error)
            return PersistOutcome(committed=False, reason=str(error))

        pages_start = 1 + len(documents)
        queue_start = pages_start + len(documents)
        page_inserts = results[pages_start:queue_start]
        queue_updates = results[queue_start : queue_start + len(queue_ids)]
        total_pages = int(results[-3].scalar(0) or 0)
        total_sites = int(results[-2].scalar(0) or 0)

        written = sum(1 for result in page_inserts if result.rows_affected > 0)
        closed = sum(1 for result in queue_updates if result.rows_affected > 0)

        outcome = PersistOutcome(
            pages_written=written,
            total_pages=total_pages,
            sites_written=total_sites,
            queue_closed=closed,
            duplicates=max(0, len(documents) - written),
            committed=True,
        )
        logger.info(
            "DB COMMIT SUCCESS: %d pages written, %d duplicates skipped, %d queue rows closed "
            "(index total: %d pages, %d sites)",
            outcome.pages_written,
            outcome.duplicates,
            outcome.queue_closed,
            outcome.total_pages,
            outcome.sites_written,
        )
        return outcome

    async def mark_failed(self, queue_ids: Sequence[int], reason: str = "") -> int:
        """Park queue rows that cannot be indexed, so the batch can move on."""
        if not queue_ids:
            return 0
        statements: list[tuple[str, Sequence[Any]]] = [
            (
                "UPDATE crawl_queue SET status = 'failed', retry_count = retry_count + 1 "
                "WHERE rowid = ?",
                (rowid,),
            )
            for rowid in queue_ids
        ]
        results = await self._client.batch(statements)
        failed = sum(1 for result in results if result.rows_affected > 0)
        if reason:
            logger.warning("marked %d queue rows failed: %s", failed, reason)
        return failed

    async def enqueue_urls(self, urls: Iterable[tuple[str, str]]) -> int:
        """Queue `(url, domain)` pairs; already-known URLs are ignored."""
        pairs = list(urls)
        if not pairs:
            return 0
        results = await self._client.batch(
            [
                (
                    "INSERT OR IGNORE INTO crawl_queue (url, domain, status) "
                    "VALUES (?, ?, 'pending')",
                    (url, domain),
                )
                for url, domain in pairs
            ]
        )
        return sum(1 for result in results if result.rows_affected > 0)

    async def rearm_processed(self, limit: int = 25, cooldown_seconds: int = 6 * 3600) -> int:
        """Return the stalest crawled URLs to `pending` once their cooldown ends.

        Without this the frontier starves: once every seed is in `pages`, no
        new seed can be enqueued and the worker idles forever. Re-arming keeps
        the crawl alive and the index fresh, bounded by a cooldown so a URL is
        not re-fetched on every pass.
        """
        result = await self._client.execute(
            "UPDATE crawl_queue SET status = 'pending', processed_at = NULL "
            "WHERE rowid IN ("
            "  SELECT rowid FROM crawl_queue"
            "  WHERE status = 'processed'"
            "    AND (processed_at IS NULL OR processed_at <= datetime('now', ?))"
            "  ORDER BY COALESCE(processed_at, '1970-01-01 00:00:00') ASC"
            "  LIMIT ?"
            ")",
            (f"-{int(cooldown_seconds)} seconds", int(limit)),
        )
        return result.rows_affected

    async def update_page(self, document: PageDocument) -> bool:
        """Refresh an existing row in place — the revalidation path."""
        result = await self._client.execute(
            "UPDATE pages SET title = ?, description = ?, body_text = ?, "
            "quality_score = ?, digest = ?, indexed_at = CURRENT_TIMESTAMP WHERE url = ?",
            (
                document.title,
                document.description,
                document.body_text,
                document.quality_score,
                document.digest,
                document.url,
            ),
        )
        return result.rows_affected > 0

    async def _rollback_quietly(self) -> None:
        try:
            await self._client.execute("ROLLBACK")
        except LibsqlError:
            # No transaction was open; nothing to undo.
            pass

    # -- queue reads -------------------------------------------------------

    async def pending_batch(self, limit: int = 10) -> list[QueuedUrl]:
        """The next `limit` pending URLs, oldest first."""
        result = await self._client.execute(
            "SELECT rowid AS id, url, domain, retry_count FROM crawl_queue "
            "WHERE status = 'pending' ORDER BY rowid LIMIT ?",
            (limit,),
        )
        return [
            QueuedUrl(
                rowid=int(row[0]),
                url=str(row[1]),
                domain=str(row[2]),
                retry_count=int(row[3] or 0),
            )
            for row in result.rows
        ]

    async def queue_depth(self) -> int:
        result = await self._client.execute(
            "SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'"
        )
        return int(result.scalar(0) or 0)

    async def known_urls(self, urls: Sequence[str]) -> set[str]:
        """Which of these URLs are already indexed or already queued."""
        if not urls:
            return set()
        placeholders = ", ".join("?" for _ in urls)
        results = await self._client.batch(
            [
                (f"SELECT url FROM pages WHERE url IN ({placeholders})", tuple(urls)),
                (f"SELECT url FROM crawl_queue WHERE url IN ({placeholders})", tuple(urls)),
            ]
        )
        known = {str(row[0]) for row in results[0].rows}
        known.update(str(row[0]) for row in results[1].rows)
        return known

    # -- search ------------------------------------------------------------

    async def search(self, query: str, limit: int = 10, offset: int = 0) -> list[SearchHit]:
        """Weighted FTS5 search, best match first."""
        match = build_match_query(query)
        if not match:
            return []

        weights = ", ".join(str(weight) for weight in BM25_WEIGHTS)
        sql = (
            "SELECT p.page_id, p.url, p.title, p.description, p.body_text, "
            f"bm25(page_fts, {weights}) AS lexical_score "
            "FROM page_fts JOIN pages p ON p.page_id = page_fts.rowid "
            "WHERE page_fts MATCH ? ORDER BY lexical_score ASC LIMIT ? OFFSET ?"
        )
        result = await self._client.execute(sql, (match, limit, offset))
        hits = [self._row_to_hit(row) for row in result.rows]

        if hits:
            return hits

        # A strict AND is right for precision and wrong for a half-remembered
        # query. Fall back to OR rather than answering "nothing found".
        loose = build_match_query(query, prefix=True)
        if loose and loose != match:
            result = await self._client.execute(sql, (loose, limit, offset))
            hits = [self._row_to_hit(row) for row in result.rows]
        return hits

    @staticmethod
    def _row_to_hit(row: Sequence[Any]) -> SearchHit:
        return SearchHit(
            page_id=int(row[0]),
            url=str(row[1] or ""),
            title=str(row[2] or ""),
            description=str(row[3] or ""),
            body_text=str(row[4] or ""),
            score=normalise_score(row[5]),
        )

    async def suggest(self, query: str, limit: int = 6) -> list[str]:
        """Title completions for the search bar."""
        match = build_match_query(query, prefix=True)
        if not match:
            return []
        result = await self._client.execute(
            "SELECT p.title FROM page_fts JOIN pages p ON p.page_id = page_fts.rowid "
            "WHERE page_fts MATCH ? AND p.title <> '' ORDER BY rank LIMIT ?",
            (match, limit),
        )
        seen: set[str] = set()
        titles: list[str] = []
        for row in result.rows:
            title = " ".join(str(row[0] or "").split())
            if title and title.lower() not in seen:
                seen.add(title.lower())
                titles.append(title)
        return titles

    async def stats(self) -> Stats:
        results = await self._client.batch(
            [
                ("SELECT COUNT(*) FROM sites", ()),
                ("SELECT COUNT(*) FROM pages", ()),
                ("SELECT COUNT(*) FROM page_fts", ()),
                ("SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'", ()),
            ]
        )
        return Stats(
            sites=int(results[0].scalar(0) or 0),
            pages=int(results[1].scalar(0) or 0),
            indexed=int(results[2].scalar(0) or 0),
            queued=int(results[3].scalar(0) or 0),
        )

    # -- revalidation ------------------------------------------------------

    async def stale_pages(self, limit: int = 5, older_than_seconds: int = 7 * 24 * 3600) -> list[str]:
        result = await self._client.execute(
            "SELECT url FROM pages WHERE indexed_at <= datetime('now', ?) "
            "ORDER BY indexed_at ASC LIMIT ?",
            (f"-{int(older_than_seconds)} seconds", limit),
        )
        return [str(row[0]) for row in result.rows]

    async def get_page(self, url: str) -> dict[str, Any] | None:
        result = await self._client.execute(
            "SELECT page_id, url, title, description, body_text FROM pages WHERE url = ?",
            (url,),
        )
        row = result.first()
        if row is None:
            return None
        return {
            "page_id": int(row[0]),
            "url": str(row[1]),
            "title": str(row[2] or ""),
            "description": str(row[3] or ""),
            "body_text": str(row[4] or ""),
        }


__all__ = [
    "BM25_WEIGHTS",
    "IndexStore",
    "PageDocument",
    "PersistOutcome",
    "QueuedUrl",
    "SearchHit",
    "Stats",
    "build_match_query",
    "fts_tokens",
    "normalise_score",
]
