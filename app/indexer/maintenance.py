"""Self-healing sweeps over rows that are already in the index.

The parser improves; the rows written before it improved do not. A page stored
under the old 40-character ceiling stays 40 characters long no matter what the
parser does today, and a page whose body opens with "Skip to main content" keeps
opening that way. Nothing re-reads them on its own.

This module is the part that notices. It is deliberately a sweep rather than a
migration: the fix is a re-crawl, not an UPDATE, because the only thing that can
produce 2 KB of clean prose is fetching the page again.

Every method takes `dry_run` and returns the same counts either way, so a pass
can be previewed before it moves anything.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: A body shorter than this was written under an older, smaller ceiling. It is
#: not necessarily thin content — it is content we truncated and never revisited.
STALE_BODY_CHARS = 300

#: Bodies that still open with the navigation phrases the parser now strips.
JUNK_OPENINGS: tuple[str, ...] = (
    "skip to main content%",
    "skip to content%",
    "skip to navigation%",
    "screen reader access%",
    "follow us on%",
    "like us on%",
)

#: Priority for a re-queued row. Lower sorts first; a repair is worth doing
#: before new discovery, because a short row is a page the reader already has.
REPAIR_PRIORITY = 0


def _junk_clause(column: str = "body_text") -> str:
    return " OR ".join(f"lower({column}) LIKE '{pattern}'" for pattern in JUNK_OPENINGS)


async def sweep_and_requeue_legacy_pages(client, *, dry_run: bool = False) -> int:
    """Push truncated or junk-opened pages back into the crawl queue.

    Returns how many rows were queued. A row already pending is re-prioritised
    rather than duplicated — `url` is unique, so the insert would fail otherwise.
    """
    stale = (
        "SELECT p.url, s.domain FROM pages p "
        "LEFT JOIN sites s ON s.site_id = p.site_id "
        f"WHERE length(coalesce(p.body_text, '')) < {STALE_BODY_CHARS} "
        f"   OR {_junk_clause('coalesce(p.body_text, \'\')')}"
    )

    counted = await client.execute(f"SELECT COUNT(*) FROM ({stale}) candidates")
    total = int(counted.scalar(0) or 0)
    if not total:
        return 0

    if dry_run:
        logger.info("dry run: %d pages would be re-queued for a clean re-crawl", total)
        return total

    result = await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, priority) "
        f"SELECT candidates.url, coalesce(candidates.domain, ''), 'pending', {REPAIR_PRIORITY} "
        f"FROM ({stale}) candidates "
        "ON CONFLICT (url) DO UPDATE "
        f"SET status = 'pending', priority = {REPAIR_PRIORITY}, retry_count = 0, last_error = NULL"
    )
    queued = int(getattr(result, "rows_affected", 0) or 0)
    logger.info("re-queued %d pages for a clean re-crawl", queued)
    return queued


async def sweep(client, *, dry_run: bool = False) -> dict[str, int]:
    """Every self-healing pass, in one call."""
    return {"requeued": await sweep_and_requeue_legacy_pages(client, dry_run=dry_run)}


__all__ = [
    "JUNK_OPENINGS",
    "REPAIR_PRIORITY",
    "STALE_BODY_CHARS",
    "sweep",
    "sweep_and_requeue_legacy_pages",
]
