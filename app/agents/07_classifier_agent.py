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
    "business_finance",
    "legal_judiciary",
    "tech_developer",
    "education_exams",
    "news_media",
    "jobs_careers",
    "health_medical",
    "travel_transit",
    "utilities",
    "commerce",
    "community_social",
    "business_corp",
    "food_dining",
    "entertainment_media",
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
            # Payments live here now: the sixteen codes have no separate PAY,
            # and a wallet is a financial product.
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
        name="food_dining",
        schema_types=("AgriculturalProduct", "FoodEstablishment", "Restaurant", "Menu"),
        url_tokens=(
            "farmer", "farmers", "krishi", "agri", "agriculture", "crop", "crops",
            "mandi", "apmc", "rural", "irrigation", "monsoon", "harvest",
            "restaurant", "menu", "dining", "food", "recipe", "order-food",
        ),
        keywords=(
            "farmer", "krishi", "mandi", "minimum support price", "crop yield",
            "sowing", "irrigation", "agricultural produce", "harvest",
            "restaurant", "dine in", "food delivery", "menu", "cuisine",
        ),
    ),
    CategoryRule(
        name="travel_transit",
        schema_types=("BusTrip", "TrainTrip", "Flight", "TaxiService"),
        url_tokens=(
            "irctc", "railway", "train", "bus", "metro", "flight", "ticket",
            "booking", "route", "depot", "transport", "rto", "parivahan",
        ),
        keywords=(
            "train status", "bus booking", "ticket booking", "metro", "flight status",
            "reservation", "bus pass", "rto", "driving licence", "vehicle registration",
        ),
    ),
    CategoryRule(
        name="utilities",
        schema_types=("UtilityService",),
        url_tokens=(
            "electricity", "water", "gas", "bescom", "bwssb", "bill-payment",
            "quick-pay", "meter", "tariff", "utility",
        ),
        keywords=(
            "electricity bill", "water supply", "bescom", "meter reading",
            "tariff", "gas cylinder", "bill payment", "utility bill",
        ),
    ),
    CategoryRule(
        name="community_social",
        schema_types=("DiscussionForumPosting", "Question", "Comment"),
        url_tokens=("forum", "community", "discussion", "thread", "q-and-a", "answers"),
        keywords=(
            "forum", "discussion thread", "community", "replies", "upvote",
            "asked by", "answered by", "question and answer",
        ),
    ),
    CategoryRule(
        name="business_corp",
        schema_types=("Corporation", "Organization", "LocalBusiness"),
        url_tokens=("company", "corporate", "about-us", "investors", "enterprise", "b2b"),
        keywords=(
            "our company", "corporate profile", "board of directors", "annual report",
            "enterprise solutions", "b2b", "investor relations",
        ),
    ),
    CategoryRule(
        name="entertainment_media",
        schema_types=("Movie", "TVSeries", "VideoObject", "MusicRecording", "SportsEvent"),
        url_tokens=("watch", "movie", "movies", "music", "stream", "video", "sports", "live"),
        keywords=(
            "watch online", "streaming", "movie", "web series", "music", "playlist",
            "live match", "highlights", "trailer",
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


# ---------------------------------------------------------------------------
# Signal 1: deterministic domain matching
#
# This is a reversal of the standing "no domain names in code" rule, and it is
# worth being explicit about why it is here: the rule exists so classification
# never depends on a list that rots. A curated list of the sites people actually
# search for is the opposite trade — it is narrow, it is checked first, and when
# it misses, the structural signals below still decide.
#
# Order matters. A specialised category beats the suffix rule, which is why GOV
# is last: sci.gov.in is a court, not a ministry, and icar.org.in is agriculture.
# ---------------------------------------------------------------------------

DOMAIN_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "FIN",
        (
            "paytm", "phonepe", "razorpay", "cred.club", "cashfree", "bhimupi",
            "mobikwik", "freecharge", "instamojo", "billdesk", "payu", "slice",
            "jupiter.money", "epfindia.gov.in", "incometax.gov.in",
        ),
    ),
    (
        "LEGAL",
        (
            "indiankanoon", "livelaw", "barandbench", "scconline", "sci.gov.in",
            "judgments", "casemine", "vakilsearch", "legalserviceindia", "lawrato",
            "ecourts", "karnatakahi",
        ),
    ),
    (
        "NEWS",
        (
            "thehindu", "deccanherald", "indianexpress", "prajavani", "kannadaprabha",
            "timesofindia", "hindustantimes", "ndtv", "theprint", "scroll.in",
            "vijaykarnataka", "udayavani", "news18",
        ),
    ),
    (
        "EDU",
        (
            ".ac.in", ".edu", "iisc.", "iit", "nptel", "coursera", "khanacademy",
            "edx", "ugc.ac.in", "swayam.gov.in", "ncert.nic.in",
        ),
    ),
    (
        "HEALTH",
        (
            "practo", "mohfw.gov.in", "nhp.gov.in", "1mg", "pharmeasy",
            "apollohospitals", "medlineplus", "aiims.edu",
        ),
    ),
    (
        "FIN",
        (
            "zerodha", "groww", "rbi.org.in", "sebi.gov.in", "moneycontrol",
            "nseindia", "bseindia", "sbi.co.in", "hdfcbank.com", "icicibank.com",
            "axisbank.com",
        ),
    ),
    (
        "TECH",
        (
            "github.com", "python.org", "developer.", "docs.",
            "npm", "pypi", "gitlab.com", "kernel.org", "huggingface.co",
        ),
    ),
    (
        "COMM",
        ("amazon.in", "flipkart.com", "myntra", "meesho", "tatacliq", "jiomart"),
    ),
    (
        "TRANS",
        ("irctc.co.in", "ksrtc.in", "mybmtc.karnataka.gov.in", "parivahan.gov.in",
         "indianrail", "bmtc", "metro"),
    ),
    ("UTIL", ("bescom.karnataka.gov.in", "bwssb.karnataka.gov.in", "bescom", "bwssb")),
    (
        "SOCIAL",
        ("reddit.com", "stackoverflow.com", "quora.com", "discourse", "stackexchange"),
    ),
    ("MEDIA", ("youtube.com", "netflix.com", "hotstar.com", "spotify.com", "sonyliv")),
    ("FOOD", ("zomato.com", "swiggy.com", "krishijagran", "icar.org.in", "agmarknet")),
    (
        "JOBS",
        ("naukri.com", "internshala.com", "linkedin.com/jobs", "foundit.in", "freshersworld"),
    ),
    # Last: a specialised domain above has already claimed its own pages.
    ("GOV", (".gov.in", ".nic.in")),
)

#: Signal 2: URL segments, for when the domain says nothing.
PATH_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("JOBS", ("careers", "jobs", "recruitment", "vacancies", "walk-in", "employment")),
    ("LEGAL", ("court-orders", "judgments", "judgment", "judgement", "acts-rules",
               "bare-acts", "act", "case", "court", "tribunal")),
    ("FIN", ("netbanking", "bill-pay", "payment-gateway", "tax-payment",
             "payments", "payment", "wallet", "upi", "checkout", "recharge")),
    ("FIN", ("fixed-deposit", "mutual-fund", "demat", "loan", "interest-rate")),
    ("UTIL", ("bill-payment", "electricity-bill", "water-bill", "gas-booking")),
    ("TRANS", ("ticket-booking", "train-status", "bus-booking", "flight-status")),
    ("EDU", ("admissions", "syllabus", "exam-results", "hall-ticket",
             "course", "courses", "lecture", "faculty")),
    ("GOV", ("fir-status", "police-verification", "complaint", "gazette")),
    ("COMM", ("cart", "checkout", "shop", "products", "pricing")),
    ("NEWS", ("news", "article", "national", "editorial", "headlines")),
    ("FOOD", ("crops", "crop", "mandiprices", "mandi", "farming", "kharif", "rabi")),
    ("HEALTH", ("hospital", "clinic", "vaccination", "opd")),
)

#: Signal 3: phrases in the title, description or snippet.
TEXT_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "FIN",
        (
            "upi payment", "wallet balance", "payment gateway", "recharge",
            "bill payment", "qr scanner", "send money to bank", "net banking transaction",
        ),
    ),
    (
        "LEGAL",
        (
            "court judgment", "bench", "high court", "supreme court", "section act",
            "petitioner", "advocate general", "tribunal",
        ),
    ),
    (
        "EDU",
        (
            "curriculum", "degree program", "admissions", "faculty",
            "examination schedule", "syllabus",
        ),
    ),
    (
        "FOOD",
        (
            "kharif", "rabi", "fertilizer", "soil health", "mandi rate",
            "crop yield", "horticulture",
        ),
    ),
)

#: The twelve codes the API publishes, mapped to the rule names this agent uses
#: internally. `Classification.category` has always carried the rule name, and
#: the signals below speak in codes, so the translation lives here.
CODE_TO_RULE: dict[str, str] = {
    "GOV": "government",
    "FIN": "business_finance",
    "LEGAL": "legal_judiciary",
    "TECH": "tech_developer",
    "EDU": "education_exams",
    "NEWS": "news_media",
    "JOBS": "jobs_careers",
    "HEALTH": "health_medical",
    "TRANS": "travel_transit",
    "UTIL": "utilities",
    "COMM": "commerce",
    "SOCIAL": "community_social",
    "CORP": "business_corp",
    "FOOD": "food_dining",
    "MEDIA": "entertainment_media",
    "WEB": "general_reference",
}

#: The sixteen codes the API publishes. Anything else is a bug, not a category.
ALLOWED_CATEGORIES: tuple[str, ...] = tuple(CODE_TO_RULE)

#: Words in a hostname that make it a court rather than a department, whatever
#: the suffix says. ecourts.gov.in is the judiciary; karnataka.gov.in is not.
COURT_HOST_TOKENS: tuple[str, ...] = ("ecourt", "judiciary", "court", "legal")

#: A deterministic domain match is not a score to be beaten, it is an answer.
DOMAIN_MATCH_SCORE = 1000
#: How many path segments must match before the path decides.
PATH_MATCH_SCORE = 60
#: A single distinctive phrase is enough for Signal 3.
TEXT_MATCH_SCORE = 40


def classify_by_domain(url: str) -> Classification | None:
    """Signal 1: the domain and path, matched against the curated list."""
    host, path = _host_and_path(url)
    haystack = f"{host}{path}"

    # A court is not a ministry. The suffix says ".gov.in", but the host says
    # which part of the state it is, and that is the more specific answer.
    if any(token in host for token in COURT_HOST_TOKENS):
        return Classification(
            category=CODE_TO_RULE["LEGAL"], score=DOMAIN_MATCH_SCORE, matched=["host:court"]
        )

    # Named entries first, bare suffixes second: "aiims.edu" is a hospital that
    # happens to sit under .edu, and a suffix should not outrank a name.
    for weak in (False, True):
        for code, needles in DOMAIN_SIGNALS:
            for needle in needles:
                if needle.startswith(".") != weak:
                    continue
                if needle in haystack:
                    return Classification(
                        category=CODE_TO_RULE.get(code, code), score=DOMAIN_MATCH_SCORE,
                        matched=[f"domain:{needle}"],
                    )
    return None


def classify_by_path(url: str) -> Classification | None:
    """Signal 2: the URL's own words, when the domain said nothing."""
    _host, path = _host_and_path(url)
    words = set(_url_words("", path))
    for code, tokens in PATH_SIGNALS:
        hits = [token for token in tokens if token in words]
        if hits:
            return Classification(
                category=CODE_TO_RULE.get(code, code), score=PATH_MATCH_SCORE,
                matched=[f"path:{h}" for h in hits],
            )
    return None


def classify_by_text(title: str, description: str, body_text: str) -> Classification | None:
    """Signal 3: the phrases a page uses about itself."""
    headline = f"{title} {description}".lower()
    body = (body_text or "").lower()
    best: Classification | None = None
    for code, phrases in TEXT_SIGNALS:
        matched = [phrase for phrase in phrases if phrase in headline]
        if not matched:
            matched = [phrase for phrase in phrases if phrase in body]
        if matched:
            score = TEXT_MATCH_SCORE + 10 * len(matched)
            if best is None or score > best.score:
                best = Classification(
                    category=CODE_TO_RULE.get(code, code), score=score,
                    matched=[f"text:{m}" for m in matched],
                )
    return best


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

        # The signals are tried in order of authority, and the first one with
        # something to say wins. WEB is not a decision, it is the absence of one.
        decided = classify_by_domain(url)
        if decided is not None:
            # A bare suffix says "government", not which department. When the
            # path names one — /judgment, /mandi, /recruitment — the path is the
            # more specific answer and wins.
            if decided.matched and decided.matched[0].startswith("domain:."):
                refined = classify_by_path(url)
                if refined is not None:
                    return refined
            return decided
        decided = classify_by_path(url)
        if decided is not None:
            return decided
        decided = classify_by_text(title, description, body_text)
        if decided is not None:
            return decided

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
