"""Turn live provider results into indexed documents.

This is the write half of "live-fetch-once, cache-forever": the first query for a
topic hits the providers, and the results it returned are converted into bounded
documents and inserted into `documents_fts` so every later query is answered
locally.

Only what the provider gave us is stored — title, URL and snippet — because
fetching each page would add a second round of network on the request path. The
crawler and the revalidation worker later replace these rows with full extracts
in place, under the same budget.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable
from urllib.parse import urlparse

from app.config import get_settings
from app.engine.config import EngineSettings, get_engine_settings
from app.engine.store import IndexStore, get_store
from app.engine.text import ExtractedDocument, detect_language, strip_artefacts, truncate
from app.engine.urls import domain_class, is_indexable, normalise_url, root_domain

logger = logging.getLogger(__name__)


def documents_from_results(
    results: Iterable[Any],
    settings: EngineSettings | None = None,
) -> list[dict[str, Any]]:
    """Convert live results into rows that fit the storage budget.

    Everything the ingest path enforces still applies: indexable URLs only, the
    title/snippet caps, and the 2 KB row budget via `fit_to_budget`.
    """
    cfg = settings or get_engine_settings()
    rows: list[dict[str, Any]] = []

    for result in list(results)[: cfg.auto_ingest_max_results]:
        url = normalise_url(str(getattr(result, "url", "") or ""))
        if not url or not is_indexable(url):
            continue

        title = truncate(strip_artefacts(str(getattr(result, "title", "") or url)), cfg.title_max_chars)
        if not title:
            continue
        snippet = truncate(strip_artefacts(str(getattr(result, "snippet", "") or "")), cfg.snippet_max_chars)
        # FTS5 needs a little more text than the snippet alone; a live hit gives us
        # only what the provider returned, so the lead is title plus snippet.
        content = truncate(f"{title}. {snippet}", cfg.content_max_chars)

        document = ExtractedDocument(
            url=url,
            canonical_url=url,
            domain=urlparse(url).netloc,
            root_domain=root_domain(url),
            domain_class=domain_class(url),
            title=title,
            snippet=snippet,
            content=content,
            language=detect_language(f"{title} {snippet}"),
            favicon_url=getattr(result, "image_url", None),
        )
        rows.append(document.fit_to_budget(cfg.max_payload_bytes).as_row())

    return rows


async def auto_ingest(
    results: Iterable[Any],
    *,
    query: str = "",
    store: IndexStore | None = None,
    settings: EngineSettings | None = None,
) -> dict[str, int]:
    """Insert live results into the index. Returns the ingest counters."""
    cfg = settings or get_engine_settings()
    if not cfg.auto_ingest_enabled:
        return {"inserted": 0, "disabled": 1}

    rows = documents_from_results(results, cfg)
    if not rows:
        return {"inserted": 0, "rejected": 1}

    outcome = await (store or get_store()).ingest_documents(rows)
    target = "Turso" if get_settings().is_remote_db else "local SQLite"
    logger.info(
        "Auto-ingested %d documents for query: %s into %s (%s)",
        int(outcome.get("inserted", 0)),
        query,
        target,
        {key: value for key, value in outcome.items() if key != "inserted"},
    )
    return outcome


__all__ = ["documents_from_results", "auto_ingest"]
