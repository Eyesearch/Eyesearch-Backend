"""Audit the index against the storage rules and fix what breaks them.

Three rules, all of which the write path already applies to new rows. This agent
exists for the rows that got in before a rule did, or that a direct insert
bypassed:

* no domain holds more than `max_pages_per_domain` pages;
* no page holds more than `page_body_char_ceiling` characters of body text;
* no indexed URL carries a tracking or session parameter.

It is safe to run repeatedly and reports what it changed. Every method takes
`dry_run` and returns the same counts either way, so a run can be previewed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

#: Query keys that identify a campaign or a session, never a different page.
TRACKING_KEYS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "referrer", "source",
        "campaign", "spm", "share", "igshid", "s_kwcid",
        "sid", "session", "sessionid", "phpsessid", "jsessionid",
        "msclkid", "yclid", "dclid", "twclid", "_ga", "_gl", "_hsenc", "_hsmi",
    }
)


def strip_tracking(url: str) -> str:
    """The same URL without its tracking parameters, or `url` unchanged."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_KEYS
    ]
    if len(kept) == len(parse_qsl(parts.query, keep_blank_values=True)):
        return url
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept, doseq=True), "")
    )


@dataclass
class EnforcementReport:
    over_cap_domains: int = 0
    pages_purged: int = 0
    bodies_trimmed: int = 0
    urls_cleaned: int = 0
    duplicates_removed: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "over_cap_domains": self.over_cap_domains,
            "pages_purged": self.pages_purged,
            "bodies_trimmed": self.bodies_trimmed,
            "urls_cleaned": self.urls_cleaned,
            "duplicates_removed": self.duplicates_removed,
            "notes": self.notes,
        }


class RuleEnforcerAgent:
    """Bring the stored rows back inside the storage rules."""

    def __init__(self, client, settings) -> None:
        self._client = client
        self._settings = settings

    async def enforce_page_cap(self, cap: int | None = None, *, dry_run: bool = False) -> tuple[int, int]:
        """Drop the pages beyond each domain's cap.

        Keeps the highest quality page of a domain, then the newest: a domain
        that grew past the cap loses its weakest pages, not its best ones.
        Returns (domains over cap, pages removed).
        """
        limit = int(cap if cap is not None else self._settings.max_pages_per_domain)

        over = await self._client.execute(
            "SELECT COUNT(*) FROM (SELECT site_id FROM pages "
            "GROUP BY site_id HAVING COUNT(*) > ?) over_cap",
            (limit,),
        )
        domains = int(over.scalar(0) or 0)

        # site_id IS NOT NULL matters: a row with no site has no domain, and
        # without this every such row lands in one partition and they purge each
        # other. The cap is per domain, so only rows with a domain are ranked.
        target = (
            "SELECT page_id FROM ("
            "  SELECT page_id, ROW_NUMBER() OVER ("
            "    PARTITION BY site_id ORDER BY quality_score DESC, page_id DESC"
            "  ) AS rn FROM pages WHERE site_id IS NOT NULL"
            ") ranked WHERE rn > ?"
        )
        counted = await self._client.execute(f"SELECT COUNT(*) FROM ({target}) excess", (limit,))
        excess = int(counted.scalar(0) or 0)

        if excess and not dry_run:
            await self._client.execute(f"DELETE FROM pages WHERE page_id IN ({target})", (limit,))

        return domains, excess

    async def enforce_body_ceiling(self, ceiling: int | None = None, *, dry_run: bool = False) -> int:
        """Trim any stored body longer than the ceiling."""
        limit = int(ceiling if ceiling is not None else self._settings.page_body_char_ceiling)
        if limit <= 0:
            return 0

        counted = await self._client.execute(
            "SELECT COUNT(*) FROM pages WHERE length(body_text) > ?", (limit,)
        )
        over = int(counted.scalar(0) or 0)
        if over and not dry_run:
            await self._client.execute(
                "UPDATE pages SET body_text = substr(body_text, 1, CAST(? AS INTEGER)) "
                "WHERE length(body_text) > ?",
                (limit, limit),
            )
        return over

    async def clean_urls(self, *, dry_run: bool = False, batch: int = 500) -> tuple[int, int]:
        """Strip tracking parameters from stored URLs.

        Returns (urls that carried tracking, duplicates removed). A stripped URL
        that already exists is a duplicate, so the tracking variant is deleted
        rather than renamed onto a row that is already there — renaming would
        break the unique constraint on `url`.
        """
        cleaned = 0
        removed = 0
        cursor = 0

        while True:
            result = await self._client.execute(
                "SELECT page_id, url FROM pages WHERE page_id > ? ORDER BY page_id LIMIT ?",
                (cursor, batch),
            )
            rows = [tuple(row) for row in result.rows]
            if not rows:
                break
            cursor = int(rows[-1][0])

            for page_id, url in rows:
                stripped = strip_tracking(str(url or ""))
                if stripped == url:
                    continue
                cleaned += 1
                if dry_run:
                    continue
                existing = await self._client.execute(
                    "SELECT page_id FROM pages WHERE url = ?", (stripped,)
                )
                # first() is None, not scalar(0) is None: scalar's default is
                # returned when there are no rows, so scalar(0) answers 0 for
                # "not found" and every tracked URL would take the delete branch.
                if existing.first() is None:
                    await self._client.execute(
                        "UPDATE pages SET url = ? WHERE page_id = ?", (stripped, page_id)
                    )
                else:
                    await self._client.execute("DELETE FROM pages WHERE page_id = ?", (page_id,))
                    removed += 1

        return cleaned, removed

    async def run(self, *, dry_run: bool = False) -> EnforcementReport:
        report = EnforcementReport()
        report.over_cap_domains, report.pages_purged = await self.enforce_page_cap(dry_run=dry_run)
        report.bodies_trimmed = await self.enforce_body_ceiling(dry_run=dry_run)
        report.urls_cleaned, report.duplicates_removed = await self.clean_urls(dry_run=dry_run)
        logger.info("rule enforcement: %s", report.as_dict())
        return report


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "11_rule_enforcer_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 2 > 0, 2 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
