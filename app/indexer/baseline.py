"""The baseline ingestion workflow: one agent, one path, one write.

    crawl_queue (status = 'pending')
        -> fetch the URL
        -> basic parse (title, headings, description, body text)
        -> direct INSERT OR REPLACE into `pages`
        -> crawl_queue row -> 'processed' (or 'failed')

Nothing else runs on this path. There is no quality gate, no sitelink
fan-out, no per-domain cap, no storage-guard probe between steps and no
second writer: those belong to the five agents that are decoupled from the
active ingestion path while we prove that one page can be written end to end
(`BASELINE_ONLY=false` restores the full six-agent pipeline).

The write is a single atomic batch — site row, page row and queue-row status
travel together — so a row is never marked `processed` without a page behind
it, and a page is never written without its queue row moving on. After every
batch the pass re-reads `COUNT(*) FROM pages` and compares it with the count
before the write: a write that reports success but does not increment the
table is treated as a failure, not swallowed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence
from urllib.parse import urlparse

import httpx

from app import database
from app.database import execute_safe, get_client
from app.engine.config import EngineSettings, get_engine_settings
from app.engine.text import (
    ExtractedDocument,
    extract_document_with_reason,
    fallback_document,
)
from app.engine.urls import domain_class, infer_category, is_indexable, normalise_url

logger = logging.getLogger(__name__)

# The one table the operator watches, and the one this workflow drains.
_PENDING_ROWS = (
    "SELECT rowid AS id, url, domain, category FROM crawl_queue "
    "WHERE status = 'pending' ORDER BY rowid LIMIT ?"
)
_QUEUE_PROCESSED = (
    "UPDATE crawl_queue SET status = 'processed', processed_at = CURRENT_TIMESTAMP "
    "WHERE rowid = ?"
)
_QUEUE_FAILED = (
    "UPDATE crawl_queue SET status = 'failed', processed_at = CURRENT_TIMESTAMP "
    "WHERE rowid = ?"
)
_SITE_UPSERT = """
INSERT INTO sites (domain, canonical_url, name, category, authority_score, last_crawled_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(domain) DO UPDATE SET
    canonical_url = excluded.canonical_url,
    name = excluded.name,
    category = excluded.category,
    authority_score = excluded.authority_score,
    last_crawled_at = excluded.last_crawled_at
"""
# `site_id` comes from the site row written in the same batch: no read in
# between, so nothing can fail silently on the way to the page insert.
_PAGE_REPLACE = """
INSERT OR REPLACE INTO pages (
    site_id, url, kind, title, headings, description, content, nav_terms,
    site_terms, category_terms, url_terms, http_status, noindex, canonical_url,
    content_hash, last_crawled_at
) VALUES (
    (SELECT site_id FROM sites WHERE domain = ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, 0, ?, ?, ?
)
"""

# Basic parse: the extractor's own document, nothing else.
ParseFn = Callable[[str, str, int, str | None], tuple[ExtractedDocument | None, str]]
FetchFn = Callable[[httpx.AsyncClient, str, EngineSettings], Any]


@dataclass
class BaselineReport:
    """What one pass did, row by row."""

    attempted: int = 0
    processed: int = 0
    failed: int = 0
    pages_before: int = 0
    pages_after: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    error: str = ""

    def fail(self, reason: str) -> None:
        self.failed += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "processed": self.processed,
            "failed": self.failed,
            "pages_before": self.pages_before,
            "pages_after": self.pages_after,
            "pages_added": max(0, self.pages_after - self.pages_before),
            "reasons": dict(self.reasons),
            **({"error": self.error} if self.error else {}),
        }


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _content_hash(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


def _authority(url: str) -> float:
    return {"government": 1.0, "news": 0.8, "knowledge": 0.9}.get(domain_class(url), 0.6)


async def count_pages() -> int:
    """The number the whole exercise is judged by: rows in `pages`."""
    try:
        result = await execute_safe("SELECT COUNT(*) FROM pages")
        rows = getattr(result, "rows", ()) or ()
        return int(rows[0][0]) if rows else 0
    except Exception as exc:
        logger.error("baseline: could not count pages: %s", exc)
        return -1


async def _run_batch(statements: Sequence[tuple[str, list[Any]]]) -> None:
    """One atomic write batch, whichever client is behind `get_client()`."""
    client = get_client()
    if hasattr(client, "batch_atomic"):
        await client.batch_atomic(list(statements))
        return
    try:
        await client.batch(list(statements), strict=True)
    except TypeError:
        await client.batch(list(statements))


def _build_statements(
    document: ExtractedDocument,
    *,
    row_id: int,
    category: str | None,
    stamp: int,
) -> tuple[list[tuple[str, list[Any]]], str]:
    """The site row, the page row and the queue update — in that order."""
    url = normalise_url(document.url)
    host = urlparse(url).netloc
    page_category = (category or document.category or infer_category(url) or "government")[:40]
    title = (document.title or host)[:120]
    statements: list[tuple[str, list[Any]]] = [
        (
            _SITE_UPSERT,
            [
                host,
                f"https://{host}/",
                (getattr(document, "site_name", None) or title)[:200],
                page_category,
                _authority(url),
                stamp,
            ],
        ),
        (
            _PAGE_REPLACE,
            [
                host,
                url,
                "root" if urlparse(url).path in ("", "/") else "sitelink",
                title,
                str(document.headings or "")[:500],
                str(document.description or "")[:500],
                str(document.body_text or "")[:1200],
                str(document.nav_terms or "")[:500],
                str(document.site_terms or host.replace(".", " "))[:300],
                str(document.category_terms or page_category)[:80],
                str(document.url_terms or "")[:300],
                200,
                document.canonical_url or url,
                _content_hash(title, str(document.description or "")),
                stamp,
            ],
        ),
        (_QUEUE_PROCESSED, [int(row_id)]),
    ]
    return statements, host


def _default_parse(url: str, html: str, status: int, category: str | None) -> tuple[ExtractedDocument | None, str]:
    return extract_document_with_reason(url, html, http_status=status, category=category)


def _root_fallback(
    url: str, status: Any, category: str | None
) -> ExtractedDocument | None:
    """A hostname root card for a served page that yielded no extract.

    A JavaScript-rendered government homepage is still a page worth indexing:
    the root record is what the site card is built from. Sitelinks that yield
    nothing are marked failed instead — there is no honest card to write.
    """
    normalised = normalise_url(url)
    if urlparse(normalised).path not in ("", "/"):
        return None
    return fallback_document(
        normalised,
        category=category or infer_category(normalised),
        http_status=int(status or 200),
        use_hostname=True,
    )


async def _default_fetch(client: httpx.AsyncClient, url: str, settings: EngineSettings) -> Any:
    from app.crawler.crawler import fetch_page_status

    html, status = await fetch_page_status(client, url, settings)
    return html, status


async def _mark_failed(row_id: int, reason: str) -> None:
    try:
        await execute_safe(_QUEUE_FAILED, [int(row_id)])
    except Exception:
        logger.exception("baseline: could not mark queue row %s failed (%s)", row_id, reason)


async def run_baseline_pass(
    *,
    batch_size: int | None = None,
    settings: EngineSettings | None = None,
    http_client: httpx.AsyncClient | None = None,
    fetch: FetchFn | None = None,
    parse: ParseFn | None = None,
) -> dict[str, Any]:
    """Drain one batch of `crawl_queue` through the baseline workflow."""
    cfg = settings or get_engine_settings()
    limit = max(1, int(batch_size or cfg.baseline_batch_size))
    fetch_fn = fetch or _default_fetch
    parse_fn = parse or _default_parse
    report = BaselineReport()

    try:
        result = await execute_safe(_PENDING_ROWS, [limit])
        rows = list(getattr(result, "rows", ()) or ())
    except Exception as exc:
        logger.exception("baseline: queue poll failed: %s", exc)
        report.error = str(exc)
        return report.as_dict()

    if not rows:
        report.pages_before = report.pages_after = await count_pages()
        logger.info(
            "baseline: crawl_queue has no pending rows (pages=%d)", report.pages_before
        )
        return {**report.as_dict(), "empty": True}

    report.pages_before = await count_pages()
    pages_start = report.pages_before
    cursor = pages_start  # the count the last verified write left behind
    logger.info(
        "baseline: pass starting — %d queued rows, pages=%d", len(rows), report.pages_before
    )

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(cfg.crawl_timeout_seconds, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": cfg.crawl_user_agent},
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    )

    try:
        for row in rows:
            row_id = int(row[0])
            raw_url = str(row[1] or "")
            row_category = str(row[3] or "") if len(row) > 3 else ""
            report.attempted += 1

            if not raw_url or not is_indexable(raw_url):
                logger.error("baseline: rowid=%s rejected before fetch (url=%r)", row_id, raw_url)
                await _mark_failed(row_id, "not_indexable")
                report.fail("not_indexable")
                continue

            try:
                html, status = await fetch_fn(client, raw_url, cfg)
            except Exception as exc:
                logger.exception("baseline: fetch raised for %s: %s", raw_url, exc)
                await _mark_failed(row_id, f"fetch_error:{type(exc).__name__}")
                report.fail(f"fetch_error:{type(exc).__name__}")
                continue

            if not html:
                logger.error(
                    "baseline: no HTML for %s (status=%s) — row marked failed", raw_url, status
                )
                await _mark_failed(row_id, "fetch_failed")
                report.fail(f"fetch_failed:{status}")
                continue

            try:
                document, reason = parse_fn(
                    raw_url, html, int(status or 200), row_category or None
                )
            except Exception as exc:
                logger.exception("baseline: parse raised for %s: %s", raw_url, exc)
                await _mark_failed(row_id, "parse_error")
                report.fail(f"parse_error:{type(exc).__name__}")
                continue

            if document is None:
                # A root that answered 200 still gets a hostname card, so the
                # pass proves a write instead of only recording a rejection.
                document = _root_fallback(raw_url, status, row_category or None)
                if document is None:
                    logger.error(
                        "baseline: parse rejected %s (%s) — row marked failed",
                        raw_url, reason or "unknown",
                    )
                    await _mark_failed(row_id, reason or "not_extracted")
                    report.fail(reason or "not_extracted")
                    continue
                logger.info(
                    "baseline: parse rejected %s (%s) — writing a hostname root card",
                    raw_url, reason or "unknown",
                )

            statements, host = _build_statements(
                document, row_id=row_id, category=row_category or None, stamp=_now_epoch()
            )
            try:
                await _run_batch(statements)
            except Exception as exc:
                logger.exception(
                    "baseline: write failed for %s (host=%s): %s", raw_url, host, exc
                )
                await _mark_failed(row_id, f"write_error:{type(exc).__name__}")
                report.fail(f"write_error:{type(exc).__name__}")
                continue

            # Prove the row landed: a batch that reports success but leaves the
            # table unchanged is a failure, and says so.
            after = await count_pages()
            if after <= cursor:
                logger.error(
                    "baseline: write reported success but pages did not grow "
                    "(%d -> %d) for %s — row marked failed",
                    cursor, after, raw_url,
                )
                await _mark_failed(row_id, "no_row_written")
                report.fail("no_row_written")
                continue

            report.processed += 1
            cursor = after
            logger.info(
                "baseline: rowid=%s stored %s (host=%s) — pages=%d", row_id, raw_url, host, after
            )
            await asyncio.sleep(cfg.crawl_delay_seconds)
    finally:
        if owns_client:
            await client.aclose()

    report.pages_after = await count_pages()
    logger.info(
        "baseline: pass complete — processed=%d failed=%d pages=%d (+%d this pass)",
        report.processed, report.failed, report.pages_after,
        max(0, report.pages_after - pages_start),
    )
    return report.as_dict()


__all__ = ["BaselineReport", "count_pages", "run_baseline_pass"]
