"""Agent 05 — the indexer.

The only module that writes pages. It asks the storage guard once per batch,
refuses to write when the guard is paused, and reports a page count only after
the database has confirmed the commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from app.engine.store import IndexStore, PageDocument, PersistOutcome
from app.indexer.guard import StorageGuard, StoragePaused, StorageState

logger = logging.getLogger(__name__)


@dataclass
class IndexOutcome:
    """What the indexer achieved, in the terms the orchestrator reports."""

    committed: bool
    pages_written: int = 0
    duplicates: int = 0
    queue_closed: int = 0
    total_pages: int = 0
    reason: str = ""
    paused: bool = False

    @property
    def counted(self) -> int:
        """Pages the caller may add to its running total.

        Only a confirmed commit can contribute, which is why this is derived
        from `committed` rather than from the number of documents offered.
        """
        return self.pages_written if self.committed else 0


class IndexerAgent:
    """Fail-closed persistence."""

    def __init__(self, store: IndexStore, guard: StorageGuard) -> None:
        self._store = store
        self._guard = guard

    async def storage_state(self) -> StorageState:
        return await self._guard.storage_state()

    async def persist(
        self,
        documents: Sequence[PageDocument],
        queue_ids: Sequence[int] = (),
    ) -> IndexOutcome:
        """Write a batch, or explain why it was not written."""
        try:
            await self._guard.assert_can_write()
        except StoragePaused as error:
            logger.warning("indexer paused: %s", error)
            return IndexOutcome(committed=False, reason=str(error), paused=True)

        outcome: PersistOutcome = await self._store.persist_batch(documents, queue_ids)
        if not outcome.committed:
            return IndexOutcome(committed=False, reason=outcome.reason)

        # The guard's cached count is now stale by exactly what we just wrote.
        self._guard.note_write(outcome.pages_written)

        return IndexOutcome(
            committed=True,
            pages_written=outcome.pages_written,
            duplicates=outcome.duplicates,
            queue_closed=outcome.queue_closed,
            total_pages=outcome.total_pages,
        )

    async def park(
        self,
        failed_ids: Sequence[int] = (),
        skipped_ids: Sequence[int] = (),
    ) -> tuple[int, int]:
        return await self._store.park_rows(failed_ids, skipped_ids)

    async def mark_failed(self, queue_ids: Sequence[int], reason: str = "") -> int:
        return await self._store.mark_failed(queue_ids, reason)

    async def mark_skipped(self, queue_ids: Sequence[int], reason: str = "") -> int:
        return await self._store.mark_skipped(queue_ids, reason)


__all__ = ["IndexOutcome", "IndexerAgent"]
