"""Agent 07 — the classifier.

Tags every page at ingestion time so a search can be narrowed without a
backfill. Ten categories, scored from three signals in descending order of
trust: the domain's public suffix, tokens in the URL, then words in the title,
description and body.

Suffixes are weighted highest because they are structural — `.gov.in` is a
government domain whatever its copy says. Body keywords are weighted lowest
because a page mentioning "bank" once in a footer is not a banking page.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Every category this agent can emit, in tie-break order.
CATEGORIES: tuple[str, ...] = (
    "government",
    "education_exams",
    "news_media",
    "jobs_careers",
    "legal_judiciary",
    "business_finance",
    "tech_developer",
    "health_medical",
    "agriculture_rural",
    "general_reference",
)

DEFAULT_CATEGORY = "general_reference"

#: Scores. A suffix outranks a single URL token, so a bare `.gov.in` page is
#: government whatever its copy says — but a page carrying an explicit topical
#: signal (a URL token plus a keyword in the title) can outrank it, which is
#: what keeps `ssc.nic.in/admit-card` out of `government`.
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


RULES: tuple[CategoryRule, ...] = (
    CategoryRule(
        name="government",
        suffixes=(".gov.in", ".nic.in", ".gov", ".nic"),
        url_tokens=("ministry", "citizen", "scheme", "department", "portal", "sarkari"),
        keywords=("government of", "citizen services", "ministry of", "public services", "gazette"),
    ),
    CategoryRule(
        name="education_exams",
        suffixes=(".ac.in", ".edu.in", ".edu"),
        url_tokens=("university", "college", "syllabus", "exam", "admit-card", "result", "admission"),
        keywords=(
            "university", "syllabus", "examination", "admit card", "semester",
            "entrance test", "academic year", "admission",
        ),
    ),
    CategoryRule(
        name="news_media",
        url_tokens=("news", "samachar", "patrike", "breaking", "editorial", "press", "headlines"),
        keywords=(
            "reported", "correspondent", "breaking news", "editorial", "press release",
            "agency reported", "bureau",
        ),
    ),
    CategoryRule(
        name="jobs_careers",
        url_tokens=("recruitment", "vacancy", "career", "jobs", "apply", "notification", "hiring"),
        keywords=(
            "recruitment", "vacancy", "apply online", "eligibility criteria",
            "last date", "pay scale", "walk-in", "applications are invited",
        ),
    ),
    CategoryRule(
        name="legal_judiciary",
        url_tokens=(
            "court", "judgment", "judgement", "act", "legal", "law", "tribunal",
            "notifications", "indiankanoon",
        ),
        keywords=(
            "supreme court", "high court", "judgment", "petitioner", "respondent",
            "section", "act, 19", "tribunal", "writ petition",
        ),
    ),
    CategoryRule(
        name="business_finance",
        url_tokens=("startup", "gst", "msme", "bank", "trade", "finance", "economy", "invest"),
        keywords=(
            "gst", "msme", "interest rate", "repo rate", "fiscal deficit",
            "quarterly results", "market capitalisation", "loan",
        ),
    ),
    CategoryRule(
        name="tech_developer",
        url_tokens=("github", "api", "software", "tech", "engineering", "python", "docs", "developer"),
        keywords=(
            "open source", "api reference", "repository", "documentation",
            "source code", "installation guide", "framework",
        ),
    ),
    CategoryRule(
        name="health_medical",
        url_tokens=("hospital", "health", "medical", "ayushman", "icmr", "aiims", "clinic", "vaccine"),
        keywords=(
            "hospital", "patient", "ayushman bharat", "vaccination", "diagnosis",
            "outpatient", "health scheme", "medical college",
        ),
    ),
    CategoryRule(
        name="agriculture_rural",
        url_tokens=("farmer", "krishi", "apmc", "mandi", "crop", "rural", "monsoon", "agri"),
        keywords=(
            "farmer", "krishi", "mandi", "minimum support price", "crop yield",
            "sowing", "irrigation", "agricultural produce",
        ),
    ),
)


@dataclass
class Classification:
    """The chosen category and why it won."""

    category: str = DEFAULT_CATEGORY
    score: int = 0
    matched: list[str] = field(default_factory=list)

    @property
    def is_fallback(self) -> bool:
        return self.category == DEFAULT_CATEGORY and not self.matched


#: Split a URL into words. Substring matching is wrong here: "example.com"
#: contains "exam" and would be classified as an exam page.
WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def _url_words(host: str, path: str) -> set[str]:
    return {word for word in WORD_SPLIT.split(f"{host} {path}") if word}


def _token_matches(token: str, words: set[str]) -> bool:
    """A multi-word token like `admit-card` needs all of its parts present."""
    parts = [part for part in token.split("-") if part]
    return bool(parts) and all(part in words for part in parts)


def _host_and_path(url: str) -> tuple[str, str]:
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host, (parts.path or "").lower()


class ClassifierAgent:
    """Assigns one of ten categories to a page."""

    def __init__(self, rules: tuple[CategoryRule, ...] = RULES) -> None:
        self._rules = rules

    def classify(
        self,
        url: str,
        title: str = "",
        description: str = "",
        body_text: str = "",
    ) -> Classification:
        host, path = _host_and_path(url)
        headline = f"{title} {description}".lower()
        body = (body_text or "").lower()

        best = Classification()
        for rule in self._rules:
            score, matched = self._score(rule, host, path, headline, body)
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
    ) -> tuple[int, list[str]]:
        score = 0
        matched: list[str] = []

        for suffix in rule.suffixes:
            if host.endswith(suffix):
                score += SUFFIX_WEIGHT
                matched.append(f"suffix:{suffix}")
                break

        words = _url_words(host, path)
        for token in rule.url_tokens:
            if _token_matches(token, words):
                score += URL_TOKEN_WEIGHT
                matched.append(f"url:{token}")

        for keyword in rule.keywords:
            if keyword in headline:
                score += TEXT_TOKEN_WEIGHT
                matched.append(f"text:{keyword}")

        body_hits = 0
        for keyword in rule.keywords:
            if body_hits >= MAX_BODY_HITS:
                break
            if keyword in body:
                score += BODY_KEYWORD_WEIGHT
                body_hits += 1
                matched.append(f"body:{keyword}")

        return score, matched


__all__ = [
    "CATEGORIES",
    "DEFAULT_CATEGORY",
    "RULES",
    "CategoryRule",
    "Classification",
    "ClassifierAgent",
]
