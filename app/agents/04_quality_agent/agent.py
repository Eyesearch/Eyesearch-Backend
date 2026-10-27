"""Agent 4 — quality: density, boilerplate and de-duplication.

The parser already applies the strict extraction gate; this agent is the second
line of defence. It rejects thin documents, block pages and repeats, and hands
the indexer a hash so the same page is never written twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .validator import MIN_DENSITY, content_hash, is_junk

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class QualityVerdict:
    ok: bool
    reason: str = ""
    digest: str = ""


class QualityAgent:
    """Async entry point: `check(document)`."""

    name = "quality"

    def __init__(self, settings: Any | None = None) -> None:
        self._settings = settings
        self._seen: set[str] = set()

    def _reset_if_needed(self) -> None:
        if len(self._seen) > 50_000:
            self._seen.clear()

    async def check(self, document: Any) -> QualityVerdict:
        try:
            title = str(getattr(document, "title", "") or "").strip()
            body = str(getattr(document, "body_text", "") or "")
            description = str(getattr(document, "description", "") or "")
            url = str(getattr(document, "url", "") or "")

            if len(title) < 3:
                return QualityVerdict(False, "no_title")
            if is_junk(title, description):
                return QualityVerdict(False, "junk_page")
            if len(body) < MIN_DENSITY:
                return QualityVerdict(False, "low_text_density")

            digest = content_hash(title, body[:200], url)
            self._reset_if_needed()
            if digest in self._seen:
                return QualityVerdict(False, "duplicate", digest)
            self._seen.add(digest)
            return QualityVerdict(True, "", digest)
        except Exception as exc:  # a validator bug must not stop the pipeline
            logger.warning("quality check failed: %s", exc)
            return QualityVerdict(False, "quality_error")

    def forget(self, digest: str) -> None:
        self._seen.discard(digest)


__all__ = ["QualityAgent", "QualityVerdict"]
