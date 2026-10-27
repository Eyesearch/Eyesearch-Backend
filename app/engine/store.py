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

import html
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.engine.config import Settings, get_settings
from app.libsql_http import LibsqlClient, LibsqlError

logger = logging.getLogger(__name__)

# Columns the FTS index carries, in bm25 weight order.
BM25_WEIGHTS = (10.0, 5.0, 1.0)

#: How much a title match is worth on top of bm25. bm25 is negative and
#: lower-is-better, so this is subtracted from the score to promote the row.
TITLE_MATCH_BOOST = 1.0

#: How many query terms are checked against the title.
MAX_TITLE_TOKENS = 5

#: Width of the passage returned as a match snippet, in characters.
SNIPPET_CHAR_CAP = 320


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
    category: str = "general_reference"
    language: str = "en"

    def as_row(self) -> tuple[Any, ...]:
        """The nine bound values the page insert expects, in SQL order.

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
            self.category,
            self.language,
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
    match_snippet: str = ""
    raw_score: float = 0.0

    @property
    def snippet(self) -> str:
        """The passage that matched the query, else the description.

        `snippet()` from FTS5 picks the part of the page the query actually
        hit, which beats a fixed prefix of the body. When the match was on the
        title the snippet is the title again, so it falls back rather than
        repeating the heading back at the reader.
        """
        matched = " ".join((self.match_snippet or "").split())
        title = " ".join((self.title or "").split())
        if matched and matched.strip("…").strip() != title:
            return matched[:SNIPPET_CHAR_CAP]

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


def passage_around_match(text: str, tokens: Sequence[str], window: int = SNIPPET_CHAR_CAP) -> str:
    """The window of `text` around the first query term it contains.

    FTS5's own `snippet()` was the obvious tool and is what the brief asked
    for, but on the SQLite build this project runs on it ignores `colnum` and
    returns column 0 for every call — verified against both a plain and an
    external-content FTS5 table, where `highlight()` behaves correctly and
    `snippet()` does not. Rather than depend on behaviour that is wrong here
    and unverifiable on Turso, the window is computed in Python: portable,
    deterministic and testable.

    Returns `""` when no token appears, so the caller can fall back to the
    description instead of showing an arbitrary opening.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned or not tokens:
        return ""

    lowered = cleaned.lower()
    positions = [lowered.find(token) for token in tokens if token]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return ""

    match_at = min(positions)
    start = max(0, match_at - window // 3)
    end = min(len(cleaned), start + window)
    start = max(0, min(start, end - window))

    # Do not cut a word in half at either edge.
    if start > 0:
        space = cleaned.find(" ", start)
        if 0 <= space < match_at:
            start = space + 1
    if end < len(cleaned):
        space = cleaned.rfind(" ", match_at, end)
        if space > match_at:
            end = space

    passage = cleaned[start:end].strip()
    if start > 0:
        passage = "…" + passage
    if end < len(cleaned):
        passage = passage + "…"
    return passage


def highlight_terms(text: str, tokens: Sequence[str]) -> str:
    """Wrap query terms in `<b>…</b>`, escaping everything else.

    Both shipped clients render a snippet as plain text and do their own
    highlighting — the web UI with `<mark>` by splitting text nodes, the Android
    app with a Spanned string. So this is **off by default** and only applied
    when a caller asks for it (`?highlight=1`); sending markup unasked would
    show literal `<b>` tags in both apps.

    The text is untrusted page content, so every segment is HTML-escaped on the
    way out and only the tags added here are real markup. The scan runs over the
    original text and escapes as it builds, so a token can never match inside an
    escaped entity like `&amp;`.
    """
    if not text:
        return text

    unique = sorted({token for token in tokens if token}, key=len, reverse=True)
    if not unique:
        return html.escape(text, quote=False)

    pattern = re.compile(
        r"\b(" + "|".join(re.escape(token) for token in unique) + r")\b",
        re.IGNORECASE,
    )

    parts: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        parts.append(html.escape(text[cursor : match.start()], quote=False))
        parts.append(f"<b>{html.escape(match.group(0), quote=False)}</b>")
        cursor = match.end()
    parts.append(html.escape(text[cursor:], quote=False))
    return "".join(parts)


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
                    "(site_id, url, title, description, body_text, quality_score, digest, "
                    "category, language, indexed_at) "
                    "VALUES ((SELECT site_id FROM sites WHERE domain = ?), ?, ?, ?, ?, ?, ?, "
                    "?, ?, CURRENT_TIMESTAMP)",
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

    async def skip_pending_for_domain(self, domain: str) -> int:
        """Park every remaining pending URL for a domain that is at its cap.

        Filtering capped domains out of the dequeue is not enough on its own:
        their rows would sit at `pending` for ever, so `queue_depth()` keeps
        reporting a full queue and `enqueue()` drops genuinely new domains at
        `MAX_PENDING_QUEUE`. Marking them `skipped` keeps the depth honest.
        """
        if not domain:
            return 0
        result = await self._client.execute(
            "UPDATE crawl_queue SET status = 'skipped' "
            "WHERE status = 'pending' AND domain = ?",
            (domain,),
        )
        return result.rows_affected

    async def skip_pending_for_capped_domains(self, cap: int | None = None) -> int:
        """One-off reconciliation for rows queued before a domain hit its cap."""
        limit = cap if cap is not None else self._settings.max_pages_per_domain
        result = await self._client.execute(
            "UPDATE crawl_queue SET status = 'skipped' WHERE status = 'pending' AND domain IN ("
            "  SELECT s.domain FROM sites s JOIN pages p ON p.site_id = s.site_id "
            "  GROUP BY s.domain HAVING COUNT(*) >= ?"
            ")",
            (limit,),
        )
        if result.rows_affected:
            logger.info(
                "marked %d queued URLs skipped: their domain is at its %d-page cap",
                result.rows_affected,
                limit,
            )
        return result.rows_affected

    async def domain_page_counts(self, domains: Sequence[str]) -> dict[str, int]:
        """Indexed pages per domain, for the whole batch in one query.

        `pages` has no `domain` column — it is reached through `sites` — and the
        count is deliberately batched: one query per batch, never one per URL,
        because a per-URL count would put a database round trip in front of
        every fetch.
        """
        unique = [domain for domain in dict.fromkeys(domains) if domain]
        if not unique:
            return {}
        placeholders = ", ".join("?" for _ in unique)
        result = await self._client.execute(
            "SELECT s.domain, COUNT(*) FROM pages p "
            "JOIN sites s ON s.site_id = p.site_id "
            f"WHERE s.domain IN ({placeholders}) GROUP BY s.domain",
            tuple(unique),
        )
        return {str(row[0]): int(row[1]) for row in result.rows}

    async def mark_skipped(self, queue_ids: Sequence[int], reason: str = "") -> int:
        """Park queue rows that are deliberately not crawled.

        Distinct from `failed`: a skipped row is a policy decision (the domain
        is at its page cap), not an error, and re-arming never revives it.
        """
        if not queue_ids:
            return 0
        results = await self._client.batch(
            [
                ("UPDATE crawl_queue SET status = 'skipped' WHERE rowid = ?", (rowid,))
                for rowid in queue_ids
            ]
        )
        skipped = sum(1 for result in results if result.rows_affected > 0)
        if reason:
            logger.info("marked %d queue rows skipped: %s", skipped, reason)
        return skipped

    async def park_rows(
        self,
        failed_ids: Sequence[int] = (),
        skipped_ids: Sequence[int] = (),
    ) -> tuple[int, int]:
        """Close failed and skipped rows in a single round trip.

        These were two separate calls, and every round trip costs a slot
        against the rate limiter that bounds the whole crawl.
        """
        statements: list[tuple[str, Sequence[Any]]] = [
            (
                "UPDATE crawl_queue SET status = 'failed', retry_count = retry_count + 1 "
                "WHERE rowid = ?",
                (rowid,),
            )
            for rowid in failed_ids
        ]
        statements.extend(
            ("UPDATE crawl_queue SET status = 'skipped' WHERE rowid = ?", (rowid,))
            for rowid in skipped_ids
        )
        if not statements:
            return (0, 0)

        results = await self._client.batch(statements)
        failed = sum(1 for result in results[: len(failed_ids)] if result.rows_affected > 0)
        skipped = sum(1 for result in results[len(failed_ids) :] if result.rows_affected > 0)
        return failed, skipped

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

    async def pending_domains(self, limit: int = 200) -> list[str]:
        """Which domains currently have work waiting."""
        result = await self._client.execute(
            "SELECT DISTINCT domain FROM crawl_queue WHERE status = 'pending' LIMIT ?",
            (limit,),
        )
        return [str(row[0]) for row in result.rows]

    async def rearm_processed(
        self,
        limit: int = 25,
        cooldown_seconds: int = 6 * 3600,
        domains: Sequence[str] | None = None,
    ) -> int:
        """Return the stalest crawled URLs to `pending` once their cooldown ends.

        Without this the frontier starves: once every seed is in `pages`, no
        new seed can be enqueued and the worker idles forever. Re-arming keeps
        the crawl alive and the index fresh, bounded by a cooldown so a URL is
        not re-fetched on every pass.
        """
        domain_clause = ""
        domain_args: list[Any] = []
        if domains:
            placeholders = ", ".join("?" for _ in domains)
            domain_clause = f" AND domain IN ({placeholders})"
            domain_args = list(domains)

        result = await self._client.execute(
            "UPDATE crawl_queue SET status = 'pending', processed_at = NULL "
            "WHERE rowid IN ("
            "  SELECT rowid FROM crawl_queue"
            "  WHERE status = 'processed'"
            "    AND (processed_at IS NULL OR processed_at <= datetime('now', ?))"
            f"{domain_clause}"
            "  ORDER BY COALESCE(processed_at, '1970-01-01 00:00:00') ASC"
            "  LIMIT ?"
            ")",
            (f"-{int(cooldown_seconds)} seconds", *domain_args, int(limit)),
        )
        return result.rows_affected

    async def update_page(self, document: PageDocument) -> bool:
        """Refresh an existing row in place — the revalidation path.

        `category` and `language` are deliberately left alone: they are set at
        ingestion and a revalidation pass would otherwise reset them to their
        defaults for every refreshed page.
        """
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
        """The next `limit` pending URLs, interleaved across domains.

        A plain `ORDER BY rowid` hands the whole batch to whichever domain was
        queued first: one portal with 40 pending URLs takes all ten slots and
        every other domain waits behind it. Numbering the rows per domain and
        ordering by that number gives round-robin — one URL from each domain,
        then a second from each — so a batch always spreads across the frontier.

        Domains that have already reached `MAX_PAGES_PER_DOMAIN` are excluded.
        Round-robin alone was not enough: once most domains are capped, every
        slot in the batch goes to a row the orchestrator will immediately skip,
        and a genuinely fresh domain queued behind them is never reached. The
        exclusion is one aggregate over `pages`; measured at 50k pages and 20k
        queued rows it is not slower than the window function it sits beside.

        The row number itself is not capped: capping it (say at two per domain)
        would shrink a batch below the configured size whenever few domains have
        work.
        """
        cap = self._settings.max_pages_per_domain
        result = await self._client.execute(
            "SELECT id, url, domain, retry_count FROM ("
            "  SELECT cq.rowid AS id, cq.url, cq.domain, cq.retry_count,"
            "         ROW_NUMBER() OVER (PARTITION BY cq.domain ORDER BY cq.rowid ASC) AS rn "
            "  FROM crawl_queue cq WHERE cq.status = 'pending'"
            "    AND cq.domain NOT IN ("
            "      SELECT s.domain FROM sites s JOIN pages p ON p.site_id = s.site_id "
            "      GROUP BY s.domain HAVING COUNT(*) >= ?"
            "    )"
            ") ORDER BY rn ASC, id ASC LIMIT ?",
            (cap, limit),
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

    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        category: str | None = None,
        language: str | None = None,
    ) -> list[SearchHit]:
        """Weighted FTS5 search, best match first, optionally filtered."""
        match = build_match_query(query)
        if not match:
            return []

        weights = ", ".join(str(weight) for weight in BM25_WEIGHTS)

        filters = ""
        extra: list[Any] = []
        if category:
            filters += " AND p.category = ?"
            extra.append(category)
        if language:
            filters += " AND p.language = ?"
            extra.append(language)

        # Title boost. Bound parameters appear in SQL order, so the title
        # terms (in the SELECT) are bound before the MATCH term (in the WHERE).
        tokens = fts_tokens(query)
        title_tokens = tokens[:MAX_TITLE_TOKENS]
        if title_tokens:
            title_terms = " OR ".join("instr(lower(p.title), ?) > 0" for _ in title_tokens)
        else:
            title_terms = "0"

        sql = (
            "SELECT p.page_id, p.url, p.title, p.description, p.body_text, "
            f"bm25(page_fts, {weights}) AS lexical_score, "
            f"CASE WHEN ({title_terms}) THEN 1 ELSE 0 END AS title_hit "
            "FROM page_fts JOIN pages p ON p.page_id = page_fts.rowid "
            f"WHERE page_fts MATCH ?{filters} "
            "ORDER BY (lexical_score - ? * title_hit) ASC LIMIT ? OFFSET ?"
        )
        args = (*title_tokens, match, *extra, TITLE_MATCH_BOOST, limit, offset)

        result = await self._client.execute(sql, args)
        hits = [self._row_to_hit(row, tokens) for row in result.rows]

        if hits:
            self._rescale_scores(hits)
            return hits

        # A strict AND is right for precision and wrong for a half-remembered
        # query. Fall back to OR rather than answering "nothing found".
        loose = build_match_query(query, prefix=True)
        if loose and loose != match:
            result = await self._client.execute(
                sql, (*title_tokens, loose, *extra, TITLE_MATCH_BOOST, limit, offset)
            )
            hits = [self._row_to_hit(row, tokens) for row in result.rows]
            self._rescale_scores(hits)
        return hits

    @staticmethod
    def _row_to_hit(row: Sequence[Any], tokens: Sequence[str] = ()) -> SearchHit:
        title_hit = int(row[6] or 0)
        boosted = float(row[5] or 0.0) - TITLE_MATCH_BOOST * title_hit
        body = str(row[4] or "")
        return SearchHit(
            page_id=int(row[0]),
            url=str(row[1] or ""),
            title=str(row[2] or ""),
            description=str(row[3] or ""),
            body_text=body,
            score=normalise_score(boosted),
            match_snippet=passage_around_match(body, tokens),
            raw_score=boosted,
        )

    @staticmethod
    def _rescale_scores(hits: Sequence[SearchHit]) -> None:
        """Spread `score` across the page of results, best first.

        bm25 is negative and lower-is-better, and the title boost pushes a
        good row below zero — so mapping it absolutely would make a boosted
        result look *worse* than an unboosted one. Rescaling against the page
        keeps the number monotone with the order the caller actually receives.
        It is relative within one page of results, not comparable across pages.
        """
        if not hits:
            return
        best = min(hit.raw_score for hit in hits)
        worst = max(hit.raw_score for hit in hits)
        span = worst - best
        for hit in hits:
            hit.score = 1.0 if span <= 0 else round((worst - hit.raw_score) / span, 4)

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
            "SELECT page_id, url, title, description, body_text, category, language "
            "FROM pages WHERE url = ?",
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
            "category": str(row[5] or "general_reference"),
            "language": str(row[6] or "en"),
        }


__all__ = [
    "BM25_WEIGHTS",
    "MAX_TITLE_TOKENS",
    "SNIPPET_CHAR_CAP",
    "TITLE_MATCH_BOOST",
    "highlight_terms",
    "passage_around_match",
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
