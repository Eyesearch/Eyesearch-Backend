"""Agent 3 — the parser: HTML in, a bounded document out.

Clean body text is capped at 1,000 characters, alongside headings (h1/h2), the meta description and up to five
sitelinks. The parser never raises: a failure is a `ParseResult` with a reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .extractors import extract_document_with_reason

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ParseResult:
    """One parsed page, or the reason it was not parsed."""

    url: str
    document: Any | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.document is not None


class ParserAgent:
    """Async entry point: `parse(url, html, status)`."""

    name = "parser"

    def __init__(self, settings: Any | None = None) -> None:
        self._settings = settings

    @property
    def settings(self) -> Any:
        if self._settings is None:
            from app.engine.config import get_engine_settings

            self._settings = get_engine_settings()
        return self._settings

    async def parse(
        self,
        url: str,
        html: str | None,
        *,
        status: int | None = 200,
        category: str | None = None,
    ) -> ParseResult:
        if not html:
            return ParseResult(url=url, reason="no_html")
        try:
            document, reason = extract_document_with_reason(
                url, html, http_status=status or 200, category=category
            )
        except Exception as exc:  # a parser crash must not stop the pipeline
            logger.warning("parse failed for %s: %s", url, exc)
            return ParseResult(url=url, reason="parse_error")

        if document is None:
            return ParseResult(url=url, reason=reason or "not_extracted")

        # Enforce a strict hard cap, regardless of environment configuration.
        limit = min(1_000, max(1, int(self.settings.body_max_chars)))
        body = str(document.body_text or "")
        if len(body) > limit:
            truncated = body[: limit - 1]
            boundary = truncated.rfind(" ")
            if boundary > 0:
                truncated = truncated[:boundary]
            document.body_text = truncated.rstrip() + "…"
        else:
            document.body_text = body
        return ParseResult(url=url, document=document)


__all__ = ["ParserAgent", "ParseResult"]
