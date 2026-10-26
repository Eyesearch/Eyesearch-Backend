"""Stale-while-revalidate: keep the index fresh without touching the request path.

Cadence by domain class:

    news portals                    every 1–2 days
    government / transport (.gov.in, .nic.in, ksrtc…)   every 7 days
    static / reference              every 15–30 days

Each recheck is a conditional GET (`If-None-Match` / `If-Modified-Since`). An
unchanged page writes nothing at all — no row update, no FTS rewrite. A changed
page is re-extracted to the same 2 KB budget and UPDATEs the existing row in
place, so a URL can never appear twice.
"""

from __future__ import annotations

import logging

import httpx

from app.crawler.crawler import fetch_page
from app.engine.config import EngineSettings, get_engine_settings
from app.engine.store import IndexStore, get_store
from app.engine.text import extract_document

logger = logging.getLogger(__name__)


class RevalidationReport(dict):
    """Counters for one pass: checked / unchanged / updated / dropped / failed."""

    def __init__(self) -> None:
        super().__init__(checked=0, unchanged=0, updated=0, dropped=0, failed=0)


async def revalidate_batch(
    limit: int | None = None,
    *,
    store: IndexStore | None = None,
    settings: EngineSettings | None = None,
    client: httpx.AsyncClient | None = None,
) -> RevalidationReport:
    """Recheck the stalest documents once. Returns the counters."""
    cfg = settings or get_engine_settings()
    index = store or get_store()
    report = RevalidationReport()
    if not cfg.revalidate_enabled:
        return report

    stale = await index.stale_documents(limit or cfg.revalidate_batch_size)
    if not stale:
        return report

    owned_client = client is None
    http = client or httpx.AsyncClient(follow_redirects=True)
    try:
        for record in stale:
            url = str(record.get("url") or "")
            if not url:
                continue
            report["checked"] += 1
            fetched = await fetch_page(
                http,
                url,
                cfg,
                etag=record.get("etag"),
                last_modified=record.get("last_modified"),
            )
            if fetched is None:
                # 304 or fetch failure: nothing to write. The worker's cooldown
                # keeps us from hammering the same URL next pass.
                report["unchanged"] += 1
                continue

            html, status = fetched
            document = extract_document(url, html, http_status=status)
            if document is None:
                report["dropped"] += 1
                continue

            updated = await index.update_document(
                url,
                document.as_row(),
                etag=None,
                last_modified=None,
            )
            if updated:
                report["updated"] += 1
                logger.info("revalidated in place: %s", url)
            else:
                report["failed"] += 1
    finally:
        if owned_client:
            await http.aclose()

    logger.info("revalidation pass: %s", dict(report))
    return report


__all__ = ["revalidate_batch", "RevalidationReport"]
