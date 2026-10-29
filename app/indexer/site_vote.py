"""Decide a site's brand, category and language from all of its pages.

One page is a sample of one. A publisher's name is often missing from a deep
article but present on the home page; a site's category is obvious from five
pages and a guess from one; a bilingual site has a dominant language that a
single page cannot show.

So the decision is a vote across the domain, not a judgement on a page. Ties go
to the most common value, then to the longest (a full publisher name beats an
abbreviation), then alphabetically, so the same input always gives the same
answer.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: A domain needs at least this many pages before a vote means anything.
MIN_PAGES_FOR_VOTE = 3


@dataclass
class SiteVote:
    domain: str
    brand: str = ""
    category: str = ""
    language: str = ""
    pages: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "brand": self.brand,
            "category": self.category,
            "language": self.language,
            "pages": self.pages,
        }


def _winner(values, *, longest_first: bool = False) -> str:
    """The most common non-empty value, with deterministic tie-breaking."""
    counts = Counter(value for value in values if value)
    if not counts:
        return ""
    if longest_first:
        return min(counts, key=lambda value: (-counts[value], -len(value), value))
    return min(counts, key=lambda value: (-counts[value], value))


def vote(domain: str, rows) -> SiteVote:
    """The consensus brand, category and language for one domain.

    `rows` is any iterable of dicts with `brand_name`, `category` and `language`.
    Pure, so a vote can be tested without a database.
    """
    rows = list(rows)
    result = SiteVote(domain=domain, pages=len(rows))
    if not rows:
        return result

    result.brand = _winner([row.get("brand_name") for row in rows], longest_first=True)
    result.category = _winner([row.get("category") for row in rows])
    result.language = _winner([row.get("language") for row in rows])
    return result


@dataclass
class VoteReport:
    domains_voted: int = 0
    brands_applied: int = 0
    categories_applied: int = 0
    languages_applied: int = 0
    votes: list[SiteVote] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "domains_voted": self.domains_voted,
            "brands_applied": self.brands_applied,
            "categories_applied": self.categories_applied,
            "languages_applied": self.languages_applied,
            "dry_run": self.dry_run,
        }


class SiteVoteAgent:
    """Apply the per-domain consensus back across that domain's pages."""

    def __init__(self, client, *, min_pages: int = MIN_PAGES_FOR_VOTE, batch: int = 500) -> None:
        self._client = client
        self.min_pages = max(1, int(min_pages))
        self._batch = max(1, batch)

    async def scan(self) -> list[SiteVote]:
        """One vote per domain, skipping domains with too few pages to vote."""
        result = await self._client.execute(
            "SELECT s.domain, p.brand_name, p.category, p.language "
            "FROM pages p JOIN sites s ON s.site_id = p.site_id "
            "WHERE s.domain IS NOT NULL ORDER BY s.domain"
        )
        grouped: dict[str, list[dict[str, str]]] = {}
        for domain, brand, category, language in result.rows:
            grouped.setdefault(str(domain), []).append(
                {
                    "brand_name": str(brand or ""),
                    "category": str(category or ""),
                    "language": str(language or ""),
                }
            )

        return [
            vote(domain, rows)
            for domain, rows in grouped.items()
            if len(rows) >= self.min_pages
        ]

    async def apply(self, votes: list[SiteVote], *, dry_run: bool = False) -> VoteReport:
        report = VoteReport(domains_voted=len(votes), votes=votes, dry_run=dry_run)

        for item in votes:
            if item.brand:
                report.brands_applied += 1
            if item.category:
                report.categories_applied += 1
            if item.language:
                report.languages_applied += 1
            if dry_run:
                continue

            # Applied as a whole-domain update: every page under the root domain
            # gets the consensus, including pages that had nothing to say.
            await self._client.execute(
                "UPDATE pages SET "
                "  brand_name = COALESCE(NULLIF(?, ''), brand_name), "
                "  category   = COALESCE(NULLIF(?, ''), category), "
                "  language   = COALESCE(NULLIF(?, ''), language) "
                "WHERE site_id = (SELECT site_id FROM sites WHERE domain = ?)",
                (item.brand, item.category, item.language, item.domain),
            )

        return report
