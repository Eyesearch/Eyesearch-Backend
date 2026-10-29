"""The daily record: what the system did, in one place.

`stats()` already answers "how many pages", and `/health` answers "is it alive".
Neither answers the question someone actually asks a week later: is the index
growing, is search getting slower, and did the janitors do anything.

This aggregates that. It reads only from what the system already records, so it
adds no bookkeeping to the hot paths.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class DailyReport:
    generated_at: str
    indexed_pages: int = 0
    domains: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    database_bytes: int = 0
    storage_fraction: float = 0.0
    search_latency_ms: float | None = None
    crawler_enabled: bool = False
    freshness_only: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "indexed_pages": self.indexed_pages,
            "domains": self.domains,
            "categories": self.categories,
            "database_bytes": self.database_bytes,
            "storage_fraction": round(self.storage_fraction, 4),
            "search_latency_ms": self.search_latency_ms,
            "crawler_enabled": self.crawler_enabled,
            "freshness_only": self.freshness_only,
            "notes": self.notes,
        }

    def as_markdown(self) -> str:
        lines = [
            f"# Eyesearch daily report — {self.generated_at}",
            "",
            f"- indexed pages: {self.indexed_pages}",
            f"- domains: {self.domains}",
            f"- database: {self.database_bytes / 1024 / 1024:.1f} MB "
            f"({self.storage_fraction * 100:.1f}% of the ceiling)",
            f"- crawler enabled: {self.crawler_enabled}",
            f"- freshness only: {self.freshness_only}",
        ]
        if self.search_latency_ms is not None:
            lines.append(f"- last search: {self.search_latency_ms:.1f} ms")
        if self.categories:
            lines.append("")
            lines.append("## Categories")
            for code, count in sorted(self.categories.items(), key=lambda item: -item[1]):
                lines.append(f"- {code}: {count}")
        return "\n".join(lines)


class ChroniclerReporterAgent:
    """Aggregate a daily report from what the system already knows."""

    def __init__(self, client, settings, orchestrator=None) -> None:
        self._client = client
        self._settings = settings
        self._orchestrator = orchestrator

    async def build(self) -> DailyReport:
        report = DailyReport(generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        report.crawler_enabled = bool(getattr(self._settings, "crawler_enabled", False))
        report.freshness_only = bool(getattr(self._orchestrator, "freshness_only", False))

        try:
            pages = await self._client.execute("SELECT COUNT(*) FROM pages")
            report.indexed_pages = int(pages.scalar(0) or 0)

            domains = await self._client.execute("SELECT COUNT(*) FROM sites")
            report.domains = int(domains.scalar(0) or 0)

            categories = await self._client.execute(
                "SELECT c.code, COUNT(*) FROM pages p "
                "LEFT JOIN categories c ON c.id = p.category_id "
                "GROUP BY c.code ORDER BY 2 DESC"
            )
            report.categories = {str(row[0] or "UNKNOWN"): int(row[1]) for row in categories.rows}

            size = await self._client.execute("SELECT pg_database_size(current_database())")
            report.database_bytes = int(size.scalar(0) or 0)
            limit = int(float(self._settings.max_storage_mb) * 1024 * 1024)
            report.storage_fraction = report.database_bytes / limit if limit else 0.0
        except Exception as error:  # noqa: BLE001 - a report must not fail the caller
            report.notes.append(f"partial report: {error}")
            logger.warning("chronicler could not read everything: %s", error)

        return report
