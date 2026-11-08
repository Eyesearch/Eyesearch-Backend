"""Remove pages the index is holding twice.

Two pages are the same page when their SHA-256 body digest matches, or when they
share a canonical path — the same host and path once the query string, a
trailing slash and a `www.` prefix are set aside.

The duplicate is chosen, not guessed: the copy with the higher quality score
survives, and the newest one breaks a tie. Deleting the wrong copy would lose
the better text, so this is deliberate rather than "keep the first row".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlsplit

logger = logging.getLogger(__name__)


def canonical_path(url: str) -> str:
    """`host/path` with the query, a trailing slash and `www.` removed."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "/").rstrip("/") or "/"
    return f"{host}{path}" if host else ""


@dataclass
class CompactionReport:
    duplicate_digests: int = 0
    duplicate_paths: int = 0
    pages_removed: int = 0
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "duplicate_digests": self.duplicate_digests,
            "duplicate_paths": self.duplicate_paths,
            "pages_removed": self.pages_removed,
            "dry_run": self.dry_run,
            "notes": self.notes,
        }


class DedupeCompactAgent:
    """Delete duplicate pages, keeping the best copy of each."""

    def __init__(self, client, batch: int = 500) -> None:
        self._client = client
        self._batch = max(1, batch)

    async def _delete_keeping_best(self, where: str, *, dry_run: bool) -> int:
        """Delete every row the ranking puts second or worse within `where`."""
        target = (
            "SELECT page_id FROM ("
            "  SELECT page_id, ROW_NUMBER() OVER ("
            f"    PARTITION BY {where} ORDER BY quality_score DESC, page_id DESC"
            "  ) AS rn FROM pages"
            ") ranked WHERE rn > 1"
        )
        counted = await self._client.execute(f"SELECT COUNT(*) FROM ({target}) dupes")
        excess = int(counted.scalar(0) or 0)
        if excess and not dry_run:
            await self._client.execute(f"DELETE FROM pages WHERE page_id IN ({target})")
        return excess

    async def by_digest(self, *, dry_run: bool = False) -> int:
        """Pages sharing a body digest: the same text under different URLs."""
        return await self._delete_keeping_best(
            "NULLIF(digest, '')", dry_run=dry_run
        )

    async def by_canonical_path(self, *, dry_run: bool = False) -> int:
        """Pages sharing a host and path once the query string is ignored.

        The key is computed here rather than in SQL because canonicalisation of
        a URL is a Python function, and duplicating it in SQL is how the two
        drift apart.
        """
        seen: dict[str, tuple[int, float, int]] = {}
        doomed: list[int] = []
        cursor = 0

        while True:
            result = await self._client.execute(
                "SELECT page_id, url, quality_score FROM pages "
                "WHERE page_id > ? ORDER BY page_id LIMIT ?",
                (cursor, self._batch),
            )
            rows = [tuple(row) for row in result.rows]
            if not rows:
                break
            cursor = int(rows[-1][0])

            for page_id, url, quality in rows:
                key = canonical_path(str(url or ""))
                if not key:
                    continue
                best = seen.get(key)
                score = float(quality or 0.0)
                if best is None:
                    seen[key] = (int(page_id), score, int(page_id))
                    continue
                best_id, best_score, newest = best
                # Higher quality wins; the newer row breaks a tie.
                if score > best_score or (score == best_score and int(page_id) > newest):
                    doomed.append(best_id)
                    seen[key] = (int(page_id), score, int(page_id))
                else:
                    doomed.append(int(page_id))

        if doomed and not dry_run:
            for start in range(0, len(doomed), self._batch):
                chunk = doomed[start : start + self._batch]
                placeholders = ", ".join("?" for _ in chunk)
                await self._client.execute(
                    f"DELETE FROM pages WHERE page_id IN ({placeholders})", tuple(chunk)
                )
        return len(doomed)

    async def run(self, *, dry_run: bool = False) -> CompactionReport:
        report = CompactionReport(dry_run=dry_run)
        digests = await self.by_digest(dry_run=dry_run)
        report.duplicate_digests = digests
        report.pages_removed += digests

        paths = await self.by_canonical_path(dry_run=dry_run)
        report.duplicate_paths = paths
        report.pages_removed += paths

        logger.info("compaction: %s", report.as_dict())
        return report


# ---------------------------------------------------------------------------
# Result clustering
#
# Deduplication at query time rather than at storage time: the same page stored
# once can still crowd a result page if one site holds eight good matches. The
# rule is two per host, in rank order.
# ---------------------------------------------------------------------------

#: How many extra matches to fetch before clustering by domain.
DOMAIN_CLUSTER_OVERFETCH = 4


def cluster_by_domain(hits, *, per_domain: int = 2, limit: int | None = None) -> list:
    """At most `per_domain` results from any one domain, in rank order.

    A search that returns eight pages from the same site is technically ranked
    and practically useless: the reader wanted the best page on the subject, not
    the site's whole navigation. Capping per domain lets the other sites through.

    Order is preserved, so this narrows the list rather than reordering it.
    """
    if per_domain <= 0:
        return list(hits)[:limit] if limit else list(hits)

    seen: dict[str, int] = {}
    kept: list = []
    for hit in hits:
        domain = _domain_of(getattr(hit, "url", "") or "")
        if seen.get(domain, 0) >= per_domain:
            continue
        seen[domain] = seen.get(domain, 0) + 1
        kept.append(hit)
        if limit is not None and len(kept) >= limit:
            break
    return kept


def _domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


class ResultClusterer:
    """The rule, as something a service can call without importing a function."""

    def cluster(self, hits, *, per_domain: int = 2, limit: int | None = None) -> list:
        return cluster_by_domain(hits, per_domain=per_domain, limit=limit)


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "13_dedupe_compact_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("MAX_RESULTS_PER_HOST", globals().get("MAX_RESULTS_PER_HOST"), _contract_rules.at_least(1), "MAX_RESULTS_PER_HOST must satisfy at_least(1)"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
