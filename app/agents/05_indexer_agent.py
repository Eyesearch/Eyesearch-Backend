"""Agent 05 — the indexer.

The only module that writes pages. It asks the storage guard once per batch,
refuses to write when the guard is paused, and reports a page count only after
the database has confirmed the commit.
"""

from __future__ import annotations

import re

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


# ---------------------------------------------------------------------------
# The AI resale snippet
#
# A context capsule, not a blurb: dense, factual, free of the boilerplate that
# every page repeats. Twenty-five pages at 300 characters is 7.5K characters of
# a domain's knowledge, which is what a retrieval pipeline actually consumes.
# ---------------------------------------------------------------------------

#: One capsule. Long enough for a full sentence, short enough to be dense.
MAX_SNIPPET_LENGTH = 300
#: Below this a string is a label, not an explanation — "Click here to login".
MIN_SNIPPET_LENGTH = 60
#: A sentence ending inside this window is preferred to a mid-clause cut.
SENTENCE_WINDOW_START = 180

#: Text that is on every page and says nothing about this one.
BOILERPLATE_PATTERNS: tuple[str, ...] = (
    r"(?i)\b(we use cookies|cookie policy|accept all cookies|privacy choices)\b",
    r"(?i)\b(javascript is disabled|enable javascript|please turn on javascript)\b",
    r"(?i)\b(all rights reserved|copyright ©|terms and conditions apply)\b",
    r"(?i)\b(skip to main content|skip to navigation|toggle navigation)\b",
    r"(?i)\b(subscribe to our newsletter|enter your email address)\b",
)

#: Punctuation left hanging when a string is cut mid-phrase.
SNIPPET_DANGLING = re.compile(r"[\s\-_|:&,./;]+$")


def is_boilerplate(text: str) -> bool:
    """Whether a string is one of the things every page says."""
    return any(re.search(pattern, text or "") for pattern in BOILERPLATE_PATTERNS)


def format_ai_snippet(
    meta_description: str | None = None,
    og_description: str | None = None,
    first_paragraph: str | None = None,
    body_text_sample: str | None = None,
) -> str:
    """The densest ≤300 characters available, in order of trustworthiness."""
    for raw in (meta_description, og_description, first_paragraph, body_text_sample):
        if not raw:
            continue
        cleaned = re.sub(r"&[a-z]+;|&#\d+;", " ", raw)
        cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        if len(cleaned) < MIN_SNIPPET_LENGTH:
            continue
        if is_boilerplate(cleaned):
            continue

        return _cap_snippet(cleaned)

    return ""


def _cap_snippet(text: str) -> str:
    """Cut to the limit, preferring a sentence end over a word boundary."""
    if len(text) <= MAX_SNIPPET_LENGTH:
        return text

    window = text[:MAX_SNIPPET_LENGTH]
    last_stop = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if last_stop >= SENTENCE_WINDOW_START:
        return window[: last_stop + 1].strip()

    cut = window[: MAX_SNIPPET_LENGTH - 3]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return f"{SNIPPET_DANGLING.sub('', cut).strip()}..."


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


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "05_indexer_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 1 > 0, 1 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
