"""Find rows that break the index's own contracts, and say whose fault it is.

Every rule below is enforced somewhere in the pipeline. Rows that break one are
either older than the rule or slipped past it, and the useful question is not
"which row" but "which step let it through" — a brand-less page is the metadata
extractor's problem, a snippet over the ceiling is the snippet step's, a body
with its vowel signs missing is the tokeniser's.

Attribution makes the audit actionable: a count per agent says where to look.
Healing is a re-queue, not a delete — the page stays searchable until the refetch
replaces it, and nothing is lost if the refetch fails.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Words below which a page is navigation rather than content.
THIN_WORD_COUNT = 15
#: A page is "supposed" to have marks if its language is one that uses them.
MARKED_LANGUAGES = frozenset({"kn", "hi", "ta", "te", "ml", "bn", "gu", "pa", "or", "ar"})

#: Which agent owns each contract. The names match the modules in app/agents.
OWNERS = {
    "empty_brand": "06_metadata_extractor",
    "snippet_too_long": "10_snippet",
    "matras_stripped": "11_tokenizer",
    "thin_content": "09_quality_guard",
}


@dataclass(frozen=True)
class Defect:
    page_id: int
    url: str
    kind: str

    @property
    def owner(self) -> str:
        return OWNERS.get(self.kind, "unknown")


@dataclass
class DefectReport:
    scanned: int = 0
    defects: list[Defect] = field(default_factory=list)
    requeued: int = 0
    dry_run: bool = False

    def by_owner(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for defect in self.defects:
            counts[defect.owner] = counts.get(defect.owner, 0) + 1
        return dict(sorted(counts.items()))

    def by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for defect in self.defects:
            counts[defect.kind] = counts.get(defect.kind, 0) + 1
        return dict(sorted(counts.items()))

    def as_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "defects": len(self.defects),
            "by_owner": self.by_owner(),
            "by_kind": self.by_kind(),
            "requeued": self.requeued,
            "dry_run": self.dry_run,
        }


#: A script that uses vowel signs should show them at roughly this rate. Real
#: Kannada or Devanagari runs well above it; text with the marks stripped runs at
#: zero. A single marked word is not evidence either way, which is why this is a
#: share rather than "any marks at all".
MIN_MARK_RATIO = 0.15


def mark_ratio(text: str) -> float:
    """The share of characters that are combining marks."""
    letters = [ch for ch in (text or "") if ch.isalpha() or unicodedata.category(ch).startswith("M")]
    if not letters:
        return 0.0
    marks = sum(1 for ch in letters if unicodedata.category(ch).startswith("M"))
    return marks / len(letters)


def classify_row(
    *,
    page_id: int,
    url: str,
    brand_name: str,
    body_text: str,
    language: str,
    ceiling: int,
    thin_words: int = THIN_WORD_COUNT,
) -> list[Defect]:
    """Every contract this row breaks. Pure, so each rule is testable alone."""
    found: list[Defect] = []

    if not (brand_name or "").strip():
        found.append(Defect(page_id, url, "empty_brand"))

    if len(body_text or "") > ceiling:
        found.append(Defect(page_id, url, "snippet_too_long"))

    words = len((body_text or "").split())
    if 0 < words < thin_words:
        found.append(Defect(page_id, url, "thin_content"))

    # A page whose language needs combining marks but has none lost them. The
    # check is on the body, not the title: a title can be a single unmarked word.
    if (
        (language or "en") in MARKED_LANGUAGES
        and len(body_text or "") > 40
        and mark_ratio(body_text) < MIN_MARK_RATIO
    ):
        found.append(Defect(page_id, url, "matras_stripped"))

    return found


class DefectAuditor:
    """Scan the index for contract breaches and re-queue the offenders."""

    def __init__(self, client, settings, batch: int = 500) -> None:
        self._client = client
        self._settings = settings
        self._batch = max(1, batch)

    async def scan(self) -> DefectReport:
        report = DefectReport()
        ceiling = int(self._settings.page_body_char_ceiling)
        cursor = 0

        while True:
            result = await self._client.execute(
                "SELECT page_id, url, brand_name, body_text, language FROM pages "
                "WHERE page_id > ? ORDER BY page_id LIMIT ?",
                (cursor, self._batch),
            )
            rows = [tuple(row) for row in result.rows]
            if not rows:
                break
            cursor = int(rows[-1][0])

            for page_id, url, brand, body, language in rows:
                report.scanned += 1
                report.defects.extend(
                    classify_row(
                        page_id=int(page_id),
                        url=str(url or ""),
                        brand_name=str(brand or ""),
                        body_text=str(body or ""),
                        language=str(language or "en"),
                        ceiling=ceiling,
                    )
                )

        return report

    async def heal(self, report: DefectReport, *, dry_run: bool = False) -> int:
        """Push the offending URLs back into the queue at priority 0."""
        if dry_run:
            return len({defect.url for defect in report.defects})

        queued = 0
        seen: set[str] = set()
        for defect in report.defects:
            if defect.url in seen:
                continue
            seen.add(defect.url)
            await self._client.execute(
                "INSERT INTO crawl_queue (url, domain, status, retry_count, priority, last_error) "
                "VALUES (?, ?, 'pending', 0, 0, ?) "
                "ON CONFLICT (url) DO UPDATE SET status = 'pending', retry_count = 0, "
                "priority = 0, processed_at = NULL, last_error = EXCLUDED.last_error",
                (defect.url, _host_of(defect.url), f"defect:{defect.kind}"),
            )
            queued += 1
        report.requeued = queued
        return queued


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(url or "").hostname or ""
    except ValueError:
        return ""
