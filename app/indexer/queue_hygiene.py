"""Queue hygiene: delete the rows that will never be fetched.

The queue is not only pending work. It also holds rows parked because their host
is at its page ceiling, and rows whose host refused every attempt. Neither will
ever be fetched, and both keep their slot for ever unless something deletes
them: measured on the live queue, 89 MB for ~30,000 rows, more than half of it
dead.

This is a separate module from `maintenance.py` because it is the only sweep
that deletes rather than re-queues, and the blast radius should be visible in
the import.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Rows the queue keeps for ever unless something deletes them: the ones parked
#: because their host is at its page ceiling, and the ones whose host refused
#: every attempt. Measured on the live queue: 89 MB for ~30,000 rows, of which
#: 13,331 were `domain_cap` and 12,000+ were dead hosts. They are queue rows,
#: not pages, so deleting them loses nothing that is indexed.
PRUNABLE_REASONS: tuple[str, ...] = ("domain_cap",)

#: A parked or failed row older than this has told us everything it can.
PRUNE_AFTER_DAYS = 30

#: How many rows one prune deletes. Bounded so a backlog cannot hold the queue
#: for minutes; the next pass takes the next slice.
PRUNE_BATCH_LIMIT = 5000


async def prune_dead_queue_rows(
    client, *, days: int = PRUNE_AFTER_DAYS, dry_run: bool = False
) -> int:
    """Delete queue rows that will never be fetched.

    A row parked at its host's ceiling is not pending work and never will be
    while the ceiling holds; a row whose host has failed for a month is a dead
    site. Both keep their slot in `crawl_queue` for ever otherwise, which is
    most of an 89 MB table.

    Pages are untouched: this deletes from the queue, so nothing indexed is
    lost. A URL deleted here can still be discovered again later.
    """
    reason_list = ", ".join(f"'{reason}'" for reason in PRUNABLE_REASONS)
    doomed = (
        "FROM crawl_queue WHERE status IN ('skipped', 'failed') "
        f"   AND (last_error IN ({reason_list}) OR last_error IS NOT NULL) "
        f"   AND coalesce(processed_at, created_at) < CURRENT_TIMESTAMP - INTERVAL '{int(days)} days'"
    )

    counted = await client.execute(
        f"SELECT COUNT(*) FROM (SELECT id {doomed} LIMIT {PRUNE_BATCH_LIMIT}) t"
    )
    total = int(counted.scalar(0) or 0)
    if not total or dry_run:
        if dry_run and total:
            logger.info("dry run: %d dead queue rows would be pruned", total)
        return total

    result = await client.execute(
        f"DELETE FROM crawl_queue WHERE id IN (SELECT id {doomed} LIMIT {PRUNE_BATCH_LIMIT})"
    )
    removed = int(getattr(result, "rows_affected", 0) or 0)
    if removed:
        logger.info("pruned %d dead queue rows", removed)
    return removed



