"""Keep every page's category in step with the current classifier rules.

Classification runs once, at ingestion. When a rule changes — a new category, a
tightened pattern — the pages already stored keep whatever the old rules said.
This agent re-runs the classifier over them and corrects the ones that disagree,
in batches, so a rule change can be applied to a live index without a rebuild.

The update writes the text column, and the database trigger derives
`category_id` from it, so the link and the name cannot disagree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class AuditReport:
    inspected: int = 0
    corrected: int = 0
    changes: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "inspected": self.inspected,
            "corrected": self.corrected,
            "changes": self.changes,
            "dry_run": self.dry_run,
        }


class CategoryAuditorAgent:
    """Re-classify stored pages and correct the misrouted ones."""

    def __init__(self, client, classifier, batch: int = 200) -> None:
        self._client = client
        self._classifier = classifier
        self._batch = max(1, batch)

    async def run(self, *, dry_run: bool = False, limit: int | None = None) -> AuditReport:
        report = AuditReport(dry_run=dry_run)
        cursor = 0

        while True:
            if limit is not None and report.inspected >= limit:
                break
            result = await self._client.execute(
                "SELECT page_id, url, title, body_text, category FROM pages "
                "WHERE page_id > ? ORDER BY page_id LIMIT ?",
                (cursor, self._batch),
            )
            rows = [tuple(row) for row in result.rows]
            if not rows:
                break
            cursor = int(rows[-1][0])

            updates: list[tuple[str, int]] = []
            for page_id, url, title, body_text, current in rows:
                report.inspected += 1
                verdict = self._classifier.classify(
                    url=str(url or ""),
                    title=str(title or ""),
                    body_text=str(body_text or ""),
                )
                if verdict.category == (current or ""):
                    continue
                updates.append((verdict.category, int(page_id)))
                key = f"{current or '(none)'} -> {verdict.category}"
                report.changes[key] = report.changes.get(key, 0) + 1

            report.corrected += len(updates)
            if updates and not dry_run:
                await self._client.batch(
                    [
                        ("UPDATE pages SET category = ? WHERE page_id = ?", update)
                        for update in updates
                    ]
                )

        logger.info("category audit: %s", report.as_dict())
        return report
