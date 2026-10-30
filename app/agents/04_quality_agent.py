"""Agent 04 — the quality gate.

A page earns a row only if it carries at least **80 characters** of real body
text and is not a known junk pattern (WAF interstitial, JavaScript shell, error
page). Body text is fingerprinted with **SHA-256**, so the same prose can never
enter the index twice under two URLs — and within a single batch the gate
rejects the repeat before it ever reaches the database.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from app.agents import load_agent
from app.engine.store import PageDocument

logger = logging.getLogger(__name__)

# The parser owns the one definition of "collapse whitespace"; reaching it
# through the loader keeps a single implementation rather than a copy.
collapse = load_agent("03_parser_agent").collapse

#: Below this many characters of body text a page is not worth a row.
MIN_BODY_CHARS = 80

#: A title shorter than this is a filename or a placeholder, not a heading.
MIN_TITLE_CHARS = 3

#: Phrases that mean the page is an interstitial, not content.
JUNK_MARKERS: tuple[str, ...] = (
    "403 forbidden",
    "access denied",
    "just a moment",
    "checking your browser",
    "enable javascript",
    "javascript is required",
    "please turn on javascript",
    "attention required! | cloudflare",
    "request blocked",
    "captcha",
    "page not found",
    "404 not found",
)


def digest_of(text: str) -> str:
    """SHA-256 of the cleaned body text, hex encoded."""
    return hashlib.sha256(collapse(text).encode("utf-8")).hexdigest()


@dataclass
class QualityVerdict:
    """The gate's decision, with the rule that made it."""

    ok: bool
    reason: str = ""
    digest: str = ""
    score: float = 1.0


class QualityAgent:
    """The anti-junk gate and in-run deduplicator."""

    def __init__(self, min_body_chars: int = MIN_BODY_CHARS) -> None:
        self.min_body_chars = min_body_chars
        self._seen: set[str] = set()

    def reset(self) -> None:
        """Forget the digests seen so far — called at the start of a batch."""
        self._seen.clear()

    def check(self, document: PageDocument) -> QualityVerdict:
        """Decide whether one document may be stored."""
        body = collapse(document.body_text)
        title = collapse(document.title)

        if len(body) < self.min_body_chars:
            return QualityVerdict(
                ok=False,
                reason="low_text_density",
                score=0.0,
            )

        lowered = body[:600].lower()
        for marker in JUNK_MARKERS:
            if marker in lowered:
                return QualityVerdict(ok=False, reason="junk_page", score=0.0)

        if len(title) < MIN_TITLE_CHARS:
            return QualityVerdict(ok=False, reason="no_title", score=0.0)

        digest = digest_of(body)
        if digest in self._seen:
            return QualityVerdict(ok=False, reason="duplicate", digest=digest, score=0.0)

        self._seen.add(digest)
        return QualityVerdict(
            ok=True,
            digest=digest,
            score=self._score(body, document),
        )

    @staticmethod
    def _score(body: str, document: PageDocument) -> float:
        """A small quality signal: more prose and a description score higher."""
        length_score = min(1.0, len(body) / 1000.0)
        description_bonus = 0.1 if document.description.strip() else 0.0
        return round(min(1.0, 0.5 + 0.4 * length_score + description_bonus), 3)


__all__ = [
    "JUNK_MARKERS",
    "MIN_BODY_CHARS",
    "MIN_TITLE_CHARS",
    "QualityAgent",
    "QualityVerdict",
    "digest_of",
]


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "04_quality_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("MIN_WORDS", globals().get("MIN_WORDS"), _contract_rules.at_least(1), "MIN_WORDS must satisfy at_least(1)"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
