"""Classify a page into one of twelve universal categories.

The rules here are pattern-based only: a TLD, a URL path token, a phrase in the
title or body, and a Schema.org type from the page's JSON-LD. No site's domain
name appears anywhere in this file — a rule that names a shop or a newspaper
would classify that site and nothing else, and would go stale the moment the
site changed.

Two ideas keep the result honest:

* **Schema.org outranks prose.** A page that says `"@type": "JobPosting"` is a
  job posting whatever its copy is about, so a schema type is weighted above
  everything except a government TLD.
* **A keyword alone is weak evidence.** "Press release" appears on corporate
  announcements that are not news, so a category can require structural evidence
  (a path token or a schema type) before its keywords are allowed to count at
  all. See `requires_structure` on the NEWS rule.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Every category this agent can emit, in priority order. Earlier wins a tie.
CATEGORIES: tuple[str, ...] = (
    "government",
    "legal_judiciary",
    "business_finance",
    "payments",
    "commerce",
    "tech_developer",
    "news_media",
    "jobs_careers",
    "education_exams",
    "health_medical",
    "agriculture_rural",
    "general_reference",
)

DEFAULT_CATEGORY = "general_reference"

#: Weights. A Schema.org type is the publisher stating what the page is; a TLD is
#: nearly as strong. Prose is the weakest signal, which is why a stray keyword
#: cannot outvote a structural one.
SCHEMA_WEIGHT = 5
SUFFIX_WEIGHT = 4
URL_TOKEN_WEIGHT = 3
TEXT_TOKEN_WEIGHT = 2
BODY_KEYWORD_WEIGHT = 1

#: How many keyword hits in the body are counted for one category.
MAX_BODY_HITS = 3


@dataclass(frozen=True)
class CategoryRule:
    """What makes a page belong to one category."""

    name: str
    suffixes: tuple[str, ...] = ()
    url_tokens: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    schema_types: tuple[str, ...] = ()
    #: When set, the keywords count only if a URL token or schema type also
    #: matched. This is what stops "press release" turning a corporate
    #: announcement into news.
    requires_structure: bool = False


RULES: tuple[CategoryRule, ...] = (
    CategoryRule(
        name="government",
        # .gov and .nic are reserved for government use in every country that
        # issues them; .mil likewise.
        suffixes=(
            ".gov", ".gov.in", ".nic", ".nic.in", ".mil", ".mil.in",
            ".gob", ".gouv", ".govt", ".gc.ca",
        ),
        schema_types=("GovernmentOrganization", "GovernmentService", "GovernmentOffice"),
        url_tokens=(
            "ministry", "department", "citizen", "scheme", "portal", "municipal",
            "panchayat", "secretariat", "public-notice", "gazette",
        ),
        keywords=(
            "government of", "ministry of", "citizen services", "public services",
            "state government", "union government", "official portal",
        ),
    ),
    CategoryRule(
        name="legal_judiciary",
        schema_types=("LegalCase", "Legislation", "CourtCase"),
        url_tokens=(
            "court", "judgment", "judgement", "tribunal", "gazette", "statute",
            "act", "law", "legal", "writ", "petition", "notifications",
        ),
        keywords=(
            "supreme court", "high court", "district court", "judgment", "judgement",
            "petitioner", "respondent", "writ petition", "tribunal", "statute",
            "act of parliament", "legal opinion", "gazette notification",
        ),
    ),
    CategoryRule(
        name="business_finance",
        schema_types=("FinancialProduct", "BankAccount", "InvestmentFund", "LoanOrCredit"),
        url_tokens=(
            "finance", "bank", "invest", "stock", "equity", "mutual-fund",
            "insurance", "tax", "gst", "market", "trading", "economy", "ipo",
        ),
        keywords=(
            "interest rate", "repo rate", "fiscal deficit", "quarterly results",
            "market capitalisation", "mutual fund", "stock exchange", "equity",
            "insurance policy", "income tax", "gst", "net worth",
        ),
    ),
    CategoryRule(
        name="payments",
        schema_types=("PaymentCharge", "PaymentService", "Invoice"),
        url_tokens=(
            "payment", "payments", "checkout", "gateway", "upi", "recharge",
            "wallet", "billing", "invoice", "pay", "remit",
        ),
        keywords=(
            "payment gateway", "make a payment", "pay online", "upi", "net banking",
            "checkout", "billing address", "transaction id", "refund policy",
        ),
    ),
    CategoryRule(
        name="commerce",
        schema_types=(
            "Product", "Offer", "AggregateOffer", "ItemAvailability", "AddToCart",
            "IndividualProduct", "ProductGroup", "Store",
        ),
        url_tokens=(
            "cart", "product", "products", "dp", "buy", "shop", "store",
            "catalogue", "catalog", "checkout", "order",
        ),
        keywords=(
            "add to cart", "buy now", "in stock", "out of stock", "free delivery",
            "price", "discount", "order now", "shipping", "cash on delivery",
        ),
    ),
    CategoryRule(
        name="tech_developer",
        schema_types=("SoftwareApplication", "SoftwareSourceCode", "TechArticle", "APIReference"),
        url_tokens=(
            "docs", "documentation", "api", "sdk", "developer", "developers",
            "reference", "repository", "source", "release", "changelog", "package",
        ),
        keywords=(
            "open source", "api reference", "repository", "documentation",
            "source code", "installation guide", "framework", "library",
            "command line", "pull request", "dependency",
        ),
    ),
    CategoryRule(
        name="news_media",
        schema_types=("NewsArticle", "ReportageNewsArticle", "AnalysisNewsArticle", "BlogPosting"),
        url_tokens=(
            "news", "breaking", "editorial", "headlines", "report", "samachar",
            "patrike", "article",
        ),
        keywords=(
            "breaking news", "correspondent", "editorial", "reported that",
            "news agency", "bureau", "press release", "announced today",
        ),
        # A press release on a company's own site is not news. Without a news
        # path or a news schema type, the keywords above do not count.
        requires_structure=True,
    ),
    CategoryRule(
        name="jobs_careers",
        schema_types=("JobPosting",),
        url_tokens=(
            "career", "careers", "jobs", "job", "recruitment", "vacancy",
            "hiring", "apply", "internship",
        ),
        keywords=(
            "recruitment", "vacancy", "apply online", "eligibility criteria",
            "last date", "pay scale", "walk-in", "applications are invited",
            "job description", "experience required",
        ),
    ),
    CategoryRule(
        name="education_exams",
        suffixes=(".edu", ".edu.in", ".ac", ".ac.in"),
        schema_types=("Course", "EducationalOrganization", "Quiz", "LearningResource"),
        url_tokens=(
            "university", "college", "school", "admission", "admissions", "exam",
            "syllabus", "result", "semester", "course", "academic", "campus",
        ),
        keywords=(
            "university", "syllabus", "examination", "admit card", "semester",
            "entrance test", "academic year", "admission", "curriculum",
        ),
    ),
    CategoryRule(
        name="health_medical",
        schema_types=("MedicalWebPage", "MedicalOrganization", "MedicalCondition", "Drug"),
        url_tokens=(
            "health", "hospital", "medical", "clinic", "doctor", "patient",
            "vaccine", "diagnosis", "treatment", "pharmacy",
        ),
        keywords=(
            "hospital", "patient", "vaccination", "diagnosis", "outpatient",
            "medical college", "health scheme", "symptoms", "treatment",
        ),
    ),
    CategoryRule(
        name="agriculture_rural",
        schema_types=("AgriculturalProduct",),
        url_tokens=(
            "farmer", "farmers", "krishi", "agri", "agriculture", "crop", "crops",
            "mandi", "apmc", "rural", "irrigation", "monsoon", "harvest",
        ),
        keywords=(
            "farmer", "krishi", "mandi", "minimum support price", "crop yield",
            "sowing", "irrigation", "agricultural produce", "harvest",
        ),
    ),
)

_RULES_BY_NAME = {rule.name: rule for rule in RULES}


@dataclass
class Classification:
    """The chosen category and why it won."""

    category: str = DEFAULT_CATEGORY
    score: int = 0
    matched: list[str] = field(default_factory=list)

    @property
    def is_default(self) -> bool:
        return self.category == DEFAULT_CATEGORY and not self.matched

    @property
    def is_fallback(self) -> bool:
        """Alias kept because callers and tests already use this name."""
        return self.is_default


def _host_and_path(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return "", ""
    return (parts.hostname or "").lower(), (parts.path or "").lower()


def _url_words(host: str, path: str) -> set[str]:
    """Every word a URL is made of, split on the separators URLs actually use."""
    blob = f"{host} {path}"
    return {word for word in re.split(r"[^a-z0-9]+", blob) if len(word) > 1}


def _token_matches(token: str, words: set[str]) -> bool:
    """A token matches a whole URL word, or a hyphenated run of them."""
    parts = token.split("-")
    if len(parts) == 1:
        return token in words
    return all(part in words for part in parts)


class ClassifierAgent:
    """Score a page against every rule and take the best one."""

    def __init__(self, rules: tuple[CategoryRule, ...] = RULES) -> None:
        self._rules = rules

    def classify(
        self,
        url: str,
        title: str = "",
        description: str = "",
        body_text: str = "",
        schema_types: list[str] | tuple[str, ...] = (),
    ) -> Classification:
        host, path = _host_and_path(url)
        headline = f"{title} {description}".lower()
        body = (body_text or "").lower()
        types = {str(item).strip().lower() for item in (schema_types or ())}

        best = Classification()
        for rule in self._rules:
            score, matched = self._score(rule, host, path, headline, body, types)
            if score > best.score:
                best = Classification(category=rule.name, score=score, matched=matched)

        return best

    @staticmethod
    def _score(
        rule: CategoryRule,
        host: str,
        path: str,
        headline: str,
        body: str,
        schema_types: set[str],
    ) -> tuple[int, list[str]]:
        score = 0
        matched: list[str] = []
        structured = False

        for suffix in rule.suffixes:
            if host.endswith(suffix):
                score += SUFFIX_WEIGHT
                matched.append(f"suffix:{suffix}")
                structured = True
                break

        for wanted in rule.schema_types:
            if wanted.lower() in schema_types:
                score += SCHEMA_WEIGHT
                matched.append(f"schema:{wanted}")
                structured = True

        words = _url_words(host, path)
        for token in rule.url_tokens:
            if _token_matches(token, words):
                score += URL_TOKEN_WEIGHT
                matched.append(f"url:{token}")
                structured = True

        # Weak evidence last, and only when the rule trusts it.
        if not (rule.requires_structure and not structured):
            for keyword in rule.keywords:
                if keyword in headline:
                    score += TEXT_TOKEN_WEIGHT
                    matched.append(f"title:{keyword}")

            hits = 0
            for keyword in rule.keywords:
                if keyword in body:
                    hits += 1
                    if hits > MAX_BODY_HITS:
                        break
                    matched.append(f"body:{keyword}")
            score += hits * BODY_KEYWORD_WEIGHT

        return score, matched


def rule_for(category: str) -> CategoryRule | None:
    """The rule behind a category name, for callers that want to explain it."""
    return _RULES_BY_NAME.get(category)


__all__ = [
    "CATEGORIES",
    "DEFAULT_CATEGORY",
    "RULES",
    "CategoryRule",
    "Classification",
    "ClassifierAgent",
    "rule_for",
]


# ---------------------------------------------------------------------------
# Site-level voting
#
# One page is a sample of one. A site's category, brand and language are
# decided by a vote across the domain, not by whichever page was parsed last.
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "07_classifier_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("CATEGORIES", globals().get("CATEGORIES"), _contract_rules.has_items(12), "CATEGORIES must satisfy has_items(12)"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
