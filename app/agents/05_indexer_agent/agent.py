"""Agent 5 — the indexer: Turso writes, FTS sync and the storage ceiling.

Writes go through the same store the search API reads, so `page_fts` is kept in
step by the schema triggers. The 300k page ceiling and the quota breaker are
checked before every write.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from .storage import storage_paused

logger = logging.getLogger(__name__)


class IndexerAgent:
    """Async entry point: `persist(documents)`."""

    name = "indexer"

    def __init__(self, store: Any | None = None, settings: Any | None = None) -> None:
        self._store = store
        self._settings = settings

    @property
    def store(self) -> Any:
        if self._store is None:
            from app.engine.store import get_store

            self._store = get_store()
        return self._store

    @property
    def settings(self) -> Any:
        if self._settings is None:
            from app.engine.config import get_engine_settings

            self._settings = get_engine_settings()
        return self._settings

    async def can_write(self) -> bool:
        """False when the storage ceiling or the quota breaker says stop."""
        try:
            return not await storage_paused(self.store, self.settings)
        except Exception:
            logger.exception("storage check failed; refusing database writes")
            return False

    async def persist(self, documents: Sequence[Any]) -> dict[str, int]:
        """Write documents, reporting a failure when no page was persisted."""
        empty = {"pages": 0, "sites": 0, "sitelinks": 0}
        if not documents:
            return empty
        if not await self.can_write():
            logger.warning("indexer paused: storage safety threshold reached")
            return {**empty, "paused": 1}

        try:
            rows = [
                doc.as_row() if hasattr(doc, "as_row") else dict(doc)
                for doc in documents
            ]
            outcome = await self.store.replace_documents(rows)
            pages = int(outcome.get("pages", 0) or 0)
            if pages <= 0:
                logger.error("index write returned no persisted pages: %s", outcome)
                return {**empty, **outcome, "pages": 0, "error": 1}

            total = await self.count()
            logger.info(
                "DB COMMIT SUCCESS: indexed %d pages (%d sites, running total %d)",
                pages, int(outcome.get("sites", 0) or 0), total,
            )
            return outcome
        except Exception:
            logger.exception("index write failed")
            return {**empty, "error": 1}

    async def count(self) -> int:
        try:
            return await self.store.count_pages()
        except Exception:
            return 0


__all__ = ["IndexerAgent"]
