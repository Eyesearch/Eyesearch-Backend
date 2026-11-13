"""The query service the frontend talks to.

One place that turns a query into the exact JSON shapes `web/config/api-config.js`
parses:

* `search()`   → the card contract `GET /v1/search`
* `suggest()`  → the OpenSearch suggestions shape `[query, [titles]]`
* `retrieve()` → the flat context contract for AI clients

Every field the cards carry is either read from the index or `null`. Nothing is
invented: no fabricated thumbnails, no synthesised publish dates, no provider
snippets passed off as our own extract.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import re
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import urlparse, urlsplit

from app.engine.config import Settings, get_settings
from app.agents import load_agent
from app.engine.store import IndexStore, SearchHit, fts_tokens, highlight_terms

CATEGORIES = load_agent("07_classifier_agent").CATEGORIES
SUPPORTED_LANGUAGES = load_agent("08_language_agent").SUPPORTED_LANGUAGES

from app.agents import load_agent

logger = logging.getLogger(__name__)

#: How many extra matches to fetch before the dedupe agent clusters them. The
#: over-fetch lives here because this is the side that does the fetching.
#: How many results to fetch for every one shown, before the per-domain cap is
#: applied. The per-domain cap needs a few spare rows to work with, and that is
#: all: widening it to ten to reach past a dominant brand doubled the cost of a
#: brand search (2.1 s to 4.3 s on "phonepe"), because every extra row is a body
#: that has to be scanned for a snippet. The other sites are now fetched by the
#: excluded leg, which is the right tool for that job.
DOMAIN_CLUSTER_OVERFETCH = 4

BreadcrumbBuilderAgent = load_agent("34_breadcrumb_builder_agent").BreadcrumbBuilderAgent
_dedupe_agent = load_agent("13_dedupe_compact_agent")
ResultClusterer = _dedupe_agent.ResultClusterer
_registrable_domain_of = _dedupe_agent._domain_of
format_company_bounded_title = load_agent("03_parser_agent").format_company_bounded_title
brand_from_domain = load_agent("10_brand_canonical_agent").brand_from_domain

SOURCE_LABEL = "eyesearch_index"

_DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx")
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")
_NEWS_HOSTS = ("thehindu.com", "deccanherald.com", "prajavani.net", "ndtv.com", "timesofindia")

_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "m.youtube.com")
_YOUTUBE_ID = re.compile(r"(?:v=|/)([A-Za-z0-9_-]{11})(?:[&?/]|$)")


#: How long a search result stays usable, and how many are kept.
CACHE_TTL_SECONDS = 60.0
CACHE_MAX_ENTRIES = 1000


#: A search slower than this is worth a log line with its breakdown.
SLOW_SEARCH_MS = 1000

#: The share of a result page the site a query names may take. Three quarters
#: leaves a quarter for the rest of the web, which is what makes a brand search
#: read as a search rather than as the site's own menu.
SITELINK_SHARE = 0.75


class SearchCache:
    """A small TTL cache for search payloads.

    Keyed on everything that changes the answer — query, limit, page, filters,
    format and whether snippets were highlighted. Entries expire after
    `ttl_seconds`, and the oldest is dropped once `max_entries` is reached.

    The lock makes this safe if a caller ever runs it off the event loop; under
    asyncio's single-threaded model it is uncontended and costs a lock acquire.
    Payloads are copied in and out so a caller mutating a response cannot
    corrupt the cached copy.
    """

    def __init__(
        self,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        max_entries: int = CACHE_MAX_ENTRIES,
        clock=time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._entries: "OrderedDict[tuple, tuple[float, dict[str, Any]]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, payload = entry
            if expires_at <= self._clock():
                del self._entries[key]
                self.misses += 1
                return None
            self.hits += 1
            return copy.deepcopy(payload)

    def set(self, key: tuple, payload: dict[str, Any]) -> None:
        with self._lock:
            self._entries[key] = (self._clock() + self._ttl, copy.deepcopy(payload))
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def invalidate_urls(self, urls) -> int:
        """Drop cached results that mention any of these URLs.

        A page that has just been re-indexed is showing stale text to anyone
        who searches for it within the TTL. Expiring the whole cache on every
        write would throw away every other query's answer too, so only the
        entries that actually contain one of the changed pages are dropped.

        Returns how many entries went.
        """
        wanted = {str(url) for url in urls if url}
        if not wanted:
            return 0

        dropped = 0
        with self._lock:
            for key in list(self._entries):
                _, payload = self._entries[key]
                results = payload.get("results") or []
                if any(str(item.get("url") or "") in wanted for item in results):
                    del self._entries[key]
                    dropped += 1
        return dropped

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


#: The short upper-case code the card chip shows for each index category. The
#: classifier's names are precise but long, and a chip has room for three or
#: four letters.
CATEGORY_CODES: dict[str, str] = {
    "government": "GOV",
    "education_exams": "EDU",
    "news_media": "NEWS",
    "jobs_careers": "JOBS",
    "legal_judiciary": "LEGAL",
    "business_finance": "FINANCE",
    "payments": "PAY",
    "commerce": "COMMERCE",
    "tech_developer": "TECH",
    "health_medical": "HEALTH",
    "agriculture_rural": "AGRI",
    "general_reference": "WEB",
}

#: The code shown when a row carries no category at all.
FALLBACK_CATEGORY_CODE = "WEB"


def category_code(category: str | None) -> str:
    """The chip label for an index category, never empty."""
    value = (category or "").strip().lower()
    if not value:
        return FALLBACK_CATEGORY_CODE
    return CATEGORY_CODES.get(value, value.upper())


@dataclass
class SearchResult:
    """One result card."""

    id: str
    title: str
    url: str
    snippet: str
    source: str = SOURCE_LABEL
    kind: str = "web"
    poster_url: str | None = None
    thumbnail_url: str | None = None
    published_at: str | None = None
    score: float = 0.0
    #: The publisher's own name, and a breadcrumb instead of a bare URL.
    brand_name: str = ""
    clean_url: str = ""
    #: Short upper-case category code, e.g. GOV or TECH.
    category: str = FALLBACK_CATEGORY_CODE
    #: The path as words a person would use, e.g. ["Health", "Notifications"].
    #: A card can show this instead of a truncated URL.
    breadcrumbs: list[str] = field(default_factory=list)
    #: Action links found on the page, e.g. [{"intent": "apply", "label":
    #: "Apply Online", "url": "..."}]. Always present, empty when there are none,
    #: so a client can read it without a null check.
    sitelinks: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "kind": self.kind,
            "poster_url": self.poster_url,
            "thumbnail_url": self.thumbnail_url,
            "published_at": self.published_at,
            "score": self.score,
            "brand_name": self.brand_name,
            "clean_url": self.clean_url,
            "category": self.category,
            "breadcrumbs": list(self.breadcrumbs),
            "sitelinks": list(self.sitelinks),
        }


#: Control characters, excluding tab/newline which collapse() handles.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: A script block, and then any tag that survived it.
_SCRIPT_BLOCK = re.compile(r"<\s*script.*?<\s*/\s*script\s*>", re.IGNORECASE | re.DOTALL)
_ANY_TAG = re.compile(r"<[^>]{0,200}>")

#: A search query is a phrase, not a document.
MAX_QUERY_CHARS = 256


def sanitise_query(text: str, limit: int = MAX_QUERY_CHARS) -> str:
    """Strip what has no business in a search query.

    Control characters and NUL bytes are removed because they break tokenisers
    and log parsers; script blocks and tags because the query is echoed back in
    the response and in the OpenSearch template, and echoing markup is how a
    reflected-XSS hole opens. Angle brackets are removed last, so nothing that
    looks like markup survives in any form.
    """
    value = str(text or "")
    value = _SCRIPT_BLOCK.sub(" ", value)
    value = _ANY_TAG.sub(" ", value)
    value = _CONTROL_CHARS.sub("", value)
    value = value.replace("\x00", "")
    value = value.replace("<", "").replace(">", "")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def _breadcrumbs(url: str) -> list[str]:
    """The path as readable words, for a card to show above the title."""
    if not url:
        return []
    try:
        return BreadcrumbBuilderAgent().build(url).as_list()
    except Exception:  # noqa: BLE001 - a breadcrumb is never worth an error
        return []


def youtube_thumbnail(url: str) -> str | None:
    """The deterministic thumbnail for a YouTube video URL, or `None`.

    `i.ytimg.com/vi/<id>/hqdefault.jpg` is a pure function of the video id, so
    this is a derived value rather than a guess.
    """
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in _YOUTUBE_HOSTS:
        return None
    match = _YOUTUBE_ID.search(url)
    if not match:
        return None
    return f"https://i.ytimg.com/vi/{match.group(1)}/hqdefault.jpg"


def kind_of(url: str) -> str:
    """A coarse content type, derived only from the URL itself."""
    path = (urlparse(url).path or "").lower()
    host = (urlparse(url).hostname or "").lower()
    if host.endswith(_YOUTUBE_HOSTS) or host == "youtu.be":
        return "video"
    if path.endswith(_DOCUMENT_EXTENSIONS):
        return "document"
    if path.endswith(_IMAGE_EXTENSIONS):
        return "image"
    if any(host.endswith(news_host) for news_host in _NEWS_HOSTS):
        return "news"
    return "web"


#: Boilerplate that Indian government portals repeat on every page — the
#: accessibility toolbar, the NIC chatbot greeting, the language picker. It is
#: real page text, so it survives extraction, but it is noise in a result card.
_LANGUAGES = (
    "English|Hindi|Kannada|Tamil|Telugu|Marathi|Bengali|Gujarati|Malayalam"
    "|Odia|Punjabi|Urdu|Assamese|Sanskrit|Konkani|Manipuri|Bodo|Dogri|Kashmiri"
    "|Sindhi|Maithili|Nepali"
)

#: The social names a civic page links in its header or footer. `X` is only
#: matched as part of a run that starts with a longer name, so a sentence
#: mentioning "plan X" is left alone.
_SOCIAL_NAMES = "Facebook|Twitter|Instagram|YouTube|LinkedIn|WhatsApp"

JUNK_PATTERNS: tuple[str, ...] = (
    r"Skip to Main Content",
    r"Skip to navigation",
    # "Screen Reader Access", "Screen Reader Hindi (हिन्दी)". It must be
    # followed by Access or a language name: a bare `[A-Za-z]+`, or both groups
    # optional, also matched "Screen Reader support" and left
    # "support is available at the helpdesk counter."
    rf"Screen Reader(?:\s+Access|\s+(?:{_LANGUAGES}))(?:\s*\([^)]{{0,30}}\))?",
    # A header/footer social row, anchored at the start so it cannot punch a
    # hole in a sentence: "Follow us on Facebook X for updates" used to become
    # "Follow us on for updates". The run must start with a longer name, so a
    # lone "X" is never stripped, and the whole row goes rather than just the
    # first two, which left "Instagram YouTube District office timings."
    rf"^\s*(?:Home\s+)?(?:{_SOCIAL_NAMES})(?:\s+(?:{_SOCIAL_NAMES}|X))*(?=\s|$)",
    r"Virtual Assistance By NIC",
    r"VANI - Virtual Assistance",
    r"I am still learning\.?\s*Please forgive me",
    r"Accessibility Controls",
    # Only the language names themselves: a loose `[^.]{0,40}` here eats the
    # next sentence, turning "... certificate online." into "ficate online.".
    rf"Select Language(?:\s+(?:{_LANGUAGES}))*",
    r"Dark Contrast Light Contrast",
    r"Resize Text",
    r"Reset Filter",
)

_JUNK_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in JUNK_PATTERNS)


def clean_display_snippet(snippet: str, title: str = "", limit: int = 300) -> str:
    """Strip navigation boilerplate from a snippet before it reaches a card.

    Display only: the stored page text, the index and the ranking are untouched,
    so a cleaned snippet never changes what a query matches or how it ranks.
    """
    if not snippet:
        return ""

    def strip_junk(text: str) -> str:
        for pattern in _JUNK_RE:
            text = pattern.sub(" ", text)
        return text

    clean = strip_junk(snippet)

    # Portals repeat the page title as the first line of the body text, and
    # sometimes twice; strip it until the snippet starts with real content.
    heading = (title or "").strip()
    for _ in range(3):
        if not heading or not clean.strip().lower().startswith(heading.lower()):
            break
        clean = clean.strip()[len(heading) :].lstrip(" :-|·")

    # Again, because removing the title can expose boilerplate that was behind it.
    clean = strip_junk(clean)

    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:limit]


def _strip_tags(text: str) -> str:
    """Remove the markup ts_headline adds, leaving the sentence intact."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


#: Below this a stored description is too short to be a sentence.
WEAK_DESCRIPTION_CHARS = 20

#: Separators that turn a description into a navigation bar: "Home | About |
#: Contact" is what the site's menu says, not what the page is about.
MENU_SEPARATORS = (" | ", " › ", " / ", " » ", " - ")


def _looks_like_a_menu(text: str) -> bool:
    body = (text or "").strip()
    if len(body) < WEAK_DESCRIPTION_CHARS:
        return True
    if len(body) > 80:
        return False
    return any(separator in body for separator in MENU_SEPARATORS)


def _host_of(url: str) -> str:
    """The site a URL belongs to, subdomains and `www.` set aside.

    The same key the clusterer groups on: `ir.phonepe.com` is `phonepe.com`
    here too, or the favoured domain would never match the group it is meant
    to favour, and one brand would be called two names.
    """
    return _registrable_domain_of(url)


def _consistent_brands(hits: Sequence[SearchHit]) -> dict[str, str]:
    """The name a site is called by, chosen once per host in this result page.

    Whichever brand the host's own pages repeat most wins, so one page whose
    `og:site_name` says "www.ksrtc" cannot rename the site on its own.
    """
    tally: dict[str, Counter[str]] = {}
    for hit in hits:
        host = _host_of(getattr(hit, "url", "") or "")
        brand = (getattr(hit, "brand_name", "") or "").strip()
        if host and brand:
            tally.setdefault(host, Counter())[brand] += 1
    return {host: counts.most_common(1)[0][0] for host, counts in tally.items()}


def to_result(hit: SearchHit, *, brand: str | None = None) -> SearchResult:
    """Map an index row onto the card contract.

    The card shows the action title — the parser's short statement of what the
    page is for — and falls back to the raw title for rows indexed before that
    field existed.
    """
    # Hybrid snippet. A navigational hit shows what the site says about itself;
    # an informational one shows the sentence from the body that mentions the
    # query, which is what the reader actually asked for. The headline arrives
    # with <b> around the match and is stripped here: both shipped clients
    # render a snippet as plain text and do their own highlighting.
    snippet_source = hit.description
    headline = _strip_tags(getattr(hit, "dynamic_headline", "") or "")
    # A navigational hit shows what the site says about itself — unless what it
    # says is "Home | About | Contact", which tells the reader nothing. A
    # description this short is a menu, not a sentence.
    weak_description = _looks_like_a_menu(hit.description or "")
    if headline and (not getattr(hit, "nav_hit", 0) or weak_description):
        snippet_source = headline

    raw_title = hit.title or hit.url
    brand = (brand or getattr(hit, "brand_name", "") or "").strip()
    # The card shows the page's own title, and the brand on its own line above
    # it — which is what the payload already carries in `brand_name`. The title
    # used to be `[Brand]: [context]`, and a search for "phonepe" came back as
    # eight cards each beginning "PhonePe:", which reads as one site repeating
    # itself rather than as eight different pages.
    # The formatter cleans the title and removes the brand where it repeats; the
    # `[Brand]: ` prefix it adds is then taken off, because the card draws the
    # brand on the line above already.
    title = format_company_bounded_title(raw_title, brand) if brand else raw_title
    if brand:
        prefix = f"{brand.lower()}:"
        if title.strip().lower().startswith(prefix):
            title = title.strip()[len(prefix):].strip()
    # A page titled exactly the brand would say it twice, once per line.
    if brand and title.strip().lower() == brand.lower():
        title = (hit.action_title or "").strip() or raw_title
    return SearchResult(
        id=f"eyesearch:{hit.page_id}",
        title=title,
        url=hit.url,
        snippet=clean_display_snippet(snippet_source or hit.snippet, title),
        kind=kind_of(hit.url),
        thumbnail_url=youtube_thumbnail(hit.url),
        score=hit.score,
        # Never empty: a card with a blank line where the publisher goes reads
        # as broken. Rows indexed before brand extraction ran have nothing
        # stored, so the domain is used as the last resort here too.
        # The resolved name for this host, not this row: the card must not
        # disagree with its neighbours about what the site is called.
        brand_name=(brand or brand_from_domain(hit.url or "") or ""),
        clean_url=hit.clean_url or "",
        # The join already knows the code; the Python map is the fallback for a
        # row whose category_id was never set.
        category=(hit.category_code or category_code(hit.category)),
        breadcrumbs=_breadcrumbs(hit.url or ""),
        sitelinks=list(hit.sitelinks or []),
    )


class FederatedFallback:
    """The external fleet, wrapped so a failure is a shrug rather than a 500.

    Everything here is ephemeral. Results are dataclasses handed straight to the
    caller; nothing is written to the store, the queue or the frontier. That is
    the air gap, enforced at the only place a search request could cross it.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """The fleet: courier, then sanitizer, converted once at the boundary.

        This used to route through `FederatedHorizonAgent`, which carried its own
        0.5-second budget and returned a single card. Both were superseded — the
        courier now asks four sources and the sanitizer aggregates them — but the
        horizon stayed in the path, so every query paid its shorter budget and
        the log line `federation exceeded 0.50s` came from a component the
        blender had already replaced.
        """
        from app.agents.federated import CrossSourceSanitizerAgent, FreeKnowledgeCourierAgent

        courier = FreeKnowledgeCourierAgent(settings=self._settings)
        cards = await courier.search(query, limit=limit)
        aggregated = CrossSourceSanitizerAgent().aggregate(cards, limit=limit)
        return [card.as_dict() for card in aggregated]


#: How long the external fleet gets before the blender gives up and answers from
#: the index alone.
#:
#: It was 800 ms. A live Wikipedia call measured 835-850 ms, so the task was
#: cancelled just before it would have answered and the panel vanished — the log
#: said "exceeded 800 ms" and nothing said the margin was the whole problem. The
#: budget is a setting now, defaulting to 1500, so it can be tuned on the VM
#: without a rebuild.
FEDERATION_TIMEOUT_MS = get_settings().federation_timeout_ms


class SearchService:
    """Search, suggest and retrieve over the index."""

    def __init__(
        self,
        store: IndexStore,
        settings: Settings | None = None,
        cache: SearchCache | None = None,
    ) -> None:
        self._store = store
        self._settings = settings or get_settings()
        self.cache = cache if cache is not None else SearchCache()
        self._federation = FederatedFallback(self._settings)

    async def search(
        self,
        query: str,
        limit: int | None = None,
        page: int = 1,
        category: str | None = None,
        language: str | None = None,
        highlight: bool = False,
    ) -> dict[str, Any]:
        """The `GET /v1/search` payload."""
        started = time.perf_counter()
        size = self._clamp_limit(limit)
        page_number = max(1, min(page, self._settings.search_max_page))
        offset = (page_number - 1) * size

        key = ("cards", query, size, page_number, category, language, highlight)
        cached = self.cache.get(key)
        if cached is not None:
            cached["cache_hit"] = True
            cached["took_ms"] = 0
            return cached

        # The federated leg starts before the index is queried, so the two run
        # concurrently rather than one after the other. It is bounded by its own
        # budget and may be cancelled; the store query is not.
        federated_task = self._start_federation(query, size) if page_number == 1 else None

        # Fetch more than asked for, then keep at most a few per domain. Without
        # the over-fetch the cap would leave the page short whenever one site
        # dominated the matches — which is exactly when it is needed.
        over_fetch = size * DOMAIN_CLUSTER_OVERFETCH
        store_started = time.perf_counter()
        try:
            hits = await self._store.search(
                query,
                limit=over_fetch,
                offset=offset,
                category=category,
                language=language,
            )
        except Exception:
            if federated_task is not None:
                federated_task.cancel()
            raise
        store_ms = int((time.perf_counter() - store_started) * 1000)
        # A query that names a site is about that site: "phonepe" should show
        # PhonePe's own pages, the way a search engine shows sitelinks, instead
        # of two of them and eight strangers. The favoured domain is the one the
        # top hit belongs to, and only when that hit is navigational - a query
        # that merely mentions a site does not earn it the slots.
        favoured = None
        if hits and getattr(hits[0], "nav_hit", 0):
            favoured = _host_of(hits[0].url)
        # Three quarters of the page for the site the reader named, the rest
        # for everybody else. A fixed number does not scale: six is most of a
        # ten-card page and a third of a twenty-card one.
        favoured_slots = max(
            self._settings.max_results_per_domain,
            math.ceil(size * SITELINK_SHARE),
        )
        hits = ResultClusterer().cluster(
            hits,
            per_domain=self._settings.max_results_per_domain,
            limit=size,
            favoured=favoured,
            favoured_per_domain=favoured_slots,
        )
        # The quarter the brand does not take must actually be filled. On a
        # brand query the pool is that brand and nothing else - 183 phonepe.com
        # pages own every match for "phonepe" - so the other seats are fetched
        # with that site excluded rather than hoped for in the same list.
        # Not gated on `favoured`: a page can fall short because one site holds
        # every match whether or not the query named it, and a short page is the
        # same problem either way. The site to step around is whichever owns the
        # top result.
        dominant = _host_of(hits[0].url) if hits else None
        if dominant and len(hits) < size:
            others = size - len(hits)
            try:
                # The cheap, unranked query: the seats being filled are the ones
                # the brand did not take, so "a few good pages from elsewhere"
                # is enough. The ranked path costs seconds on a brand name.
                extra_hits = await self._store.search_other_sites(
                    query,
                    exclude_domain=dominant,
                    limit=others * DOMAIN_CLUSTER_OVERFETCH,
                    category=category,
                    language=language,
                )
            except Exception:  # noqa: BLE001 - the page is still good without it
                extra_hits = []
            if extra_hits:
                seen_urls = {getattr(hit, "url", "") for hit in hits}
                fresh = [hit for hit in extra_hits if getattr(hit, "url", "") not in seen_urls]
                hits = ResultClusterer().cluster(
                    hits + fresh,
                    per_domain=self._settings.max_results_per_domain,
                    limit=size,
                    favoured=favoured,
                    favoured_per_domain=favoured_slots,
                )
        # One brand per site, not per page: a result page that calls one site
        # two names reads as two sites.
        brand_by_host = _consistent_brands(hits)
        results = [
            to_result(hit, brand=brand_by_host.get(_host_of(hit.url))).as_dict()
            for hit in hits
        ]

        blended = await self._blend(query, results, federated_task)

        if highlight:
            tokens = fts_tokens(query)
            for result in results:
                result["snippet"] = highlight_terms(result["snippet"], tokens)

        took_ms = int((time.perf_counter() - started) * 1000)
        # Where the time went, when it went somewhere it should not have. A
        # search that takes six seconds is either a slow query or slow work
        # around it, and the two are indistinguishable from outside.
        if took_ms >= SLOW_SEARCH_MS:
            logger.info(
                "slow search: query=%r store_ms=%d rest_ms=%d hits=%d took_ms=%d",
                query,
                store_ms,
                took_ms - store_ms,
                len(hits),
                took_ms,
            )

        # `results` stays the organic list. The frontend renders it today, and a
        # card from a source we never crawled must not appear in the slot that
        # means "we verified this page".
        knowledge_cards = blended.get("knowledge_cards", [])
        primary_card = blended.get("primary_card")
        federated = bool(knowledge_cards)

        payload = {
            "query": query,
            "page": page_number,
            "limit": size,
            "total_estimate": len(results),
            # The fallback only ever answers page one. Saying so in the envelope
            # is what lets the frontend disable Next instead of offering a page
            # that will always come back empty.
            "total_pages": 1 if federated else max(1, -(-len(results) // max(1, size))),
            "has_next_page": False if federated else len(results) >= size,
            "took_ms": took_ms,
            # The ranked query ran past the statement timeout and the index-only
            # path answered instead. Say so: under crawl load this is why an
            # answer looks worse than the same query did on an idle box.
            "degraded": any(getattr(hit, "degraded", False) for hit in hits),
            "cache_hit": False,
            "providers": {SOURCE_LABEL: len(results)},
            "failed_providers": [],
            "filters": {"category": category, "language": language},
            "results": results,
            # The rich panel, additive: a consumer that ignores these keys sees
            # exactly what it saw before.
            "knowledge_cards": knowledge_cards,
            "primary_card": primary_card,
            "blend": blended.get("meta", {}),
        }
        self.cache.set(key, payload)
        return payload

    def _start_federation(self, query: str, size: int):
        """Kick off the federated leg without awaiting it.

        Started before the index query so the two overlap. Returns None when
        federation is off, which the blender reads as "no external leg at all"
        rather than "an external leg that failed".
        """
        if not self._settings.federation_enabled:
            return None
        try:
            return asyncio.ensure_future(self._federation.search(query, limit=size))
        except Exception as error:  # noqa: BLE001 - a fault here costs a panel
            logger.warning("federation could not start: %s", error)
            return None

    async def _blend(self, query: str, organic: list[dict[str, Any]], federated_task) -> dict[str, Any]:
        """Merge the two legs. The organic list is passed through untouched."""
        from app.agents.federated import KnowledgeBlender

        blender = KnowledgeBlender(timeout_ms=FEDERATION_TIMEOUT_MS)
        if federated_task is None:
            return await blender.blend(query, organic, None)
        # The task already exists, so the factory hands the blender the running
        # one rather than starting a second.
        return await blender.blend(query, organic, lambda: federated_task)

    async def _federated_fallback(self, query: str, size: int) -> list[dict[str, Any]]:
        """Ask the external fleet, with a hard ceiling on how long it may take.

        A fallback that can hang is worse than no fallback: the reader waits for
        a page that was already empty. 800 ms is enough for a provider that is
        going to answer and short enough that one that is not costs a blink.
        """
        if not self._settings.federation_enabled:
            return []

        # An explicit task, so a timeout can cancel it. `wait_for` cancels the
        # coroutine it wraps, but a task created inside the courier can survive
        # the wait and keep its socket open; cancelling here is what actually
        # releases it.
        task = asyncio.ensure_future(self._federation.search(query, limit=size))
        try:
            cards = await asyncio.wait_for(task, timeout=FEDERATION_TIMEOUT_MS / 1000.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            logger.info("federation timed out after %d ms; task cancelled", FEDERATION_TIMEOUT_MS)
            return []
        except Exception as error:  # noqa: BLE001 - a fallback must never 500
            logger.warning("federation failed: %s", error)
            return []

        # Already dicts by now — `FederatedFallback` did the conversion. Doing
        # it again here would be the second conversion, and the one that breaks.
        return [dict(card) for card in cards or []][:size]

    async def search_flat(
        self,
        query: str,
        limit: int | None = None,
        category: str | None = None,
        language: str | None = None,
        highlight: bool = False,
    ) -> dict[str, Any]:
        """The meta-search contract (`format=json`), shaped like the README."""
        started = time.perf_counter()
        size = self._clamp_limit(limit)

        key = ("json", query, size, category, language, highlight)
        cached = self.cache.get(key)
        if cached is not None:
            cached["cache_hit"] = True
            cached["took_ms"] = 0
            return cached

        hits = await self._store.search(
            query, limit=size, category=category, language=language
        )
        tokens = fts_tokens(query) if highlight else []

        payload = {
            "query": query,
            "total_hits": len(hits),
            "took_ms": int((time.perf_counter() - started) * 1000),
            "source": SOURCE_LABEL,
            "cache_hit": False,
            "results": [
                {
                    "title": hit.title or hit.url,
                    "url": hit.url,
                    "snippet": highlight_terms(hit.snippet, tokens) if highlight else hit.snippet,
                    "favicon": self._favicon(hit.url),
                }
                for hit in hits
            ],
        }
        self.cache.set(key, payload)
        return payload

    def invalidate(self, urls) -> int:
        """Forget cached answers that contain these URLs.

        Called after a batch is written, so a page that has just been
        re-indexed stops being served from the cache in its old form.
        """
        return self.cache.invalidate_urls(urls)

    async def suggest(self, query: str, limit: int = 6) -> list[Any]:
        """The OpenSearch suggestions shape: `[query, [completions]]`.

        Answered from the in-memory trie when one has been built. Suggest is the
        most frequent request the system serves — it fires on every keystroke —
        and answering it from PostgreSQL meant a round trip per character typed.
        The trie is one pointer per character and touches no database.

        The database path stays as the fallback: the trie is built by the
        maintenance loop and is absent until that first pass has run, and a
        suggestion is a convenience that must never turn into an error.
        """
        trie = getattr(self, "trie", None)
        if trie is not None:
            try:
                completions = trie.suggest(query, limit)
                if completions:
                    return [query, completions]
            except Exception as error:  # noqa: BLE001 - fall through to the database
                logger.debug("trie suggest failed for %r: %s", query, error)

        try:
            titles = await self._store.suggest(query, limit=limit)
        except Exception as error:  # suggestions are a convenience, never an error
            logger.debug("suggest failed for %r: %s", query, error)
            return [query, []]
        return [query, titles]

    async def retrieve(
        self,
        query: str,
        limit: int | None = None,
        format: str = "json",
    ) -> dict[str, Any] | str:
        """Token-optimised context for AI clients."""
        size = self._clamp_limit(limit)
        hits = await self._store.search(query, limit=size)
        results = [
            {"title": hit.title or hit.url, "url": hit.url, "snippet": hit.snippet}
            for hit in hits
        ]
        if format == "markdown":
            return self._as_markdown(query, results)
        return {
            "query": query,
            "format": format,
            "degraded": False,
            "providers": {SOURCE_LABEL: len(results)},
            "failed_providers": [],
            "results": results,
            "context": [
                {"title": hit.title or hit.url, "url": hit.url, "text": hit.snippet}
                for hit in hits
            ],
        }

    async def stats(self) -> dict[str, Any]:
        stats = await self._store.stats()
        return stats.as_dict()

    @staticmethod
    def normalise_category(category: str | None) -> str | None:
        """Validate a `category` filter, or `None` when it was not supplied."""
        if category is None or not category.strip():
            return None
        value = category.strip().lower()
        if value in CATEGORIES:
            return value
        # The UI shows GOV/TECH/..., so accept what the user just read on a chip.
        for name, code in CATEGORY_CODES.items():
            if value == code.lower():
                return name
        raise ValueError(f"unknown category '{category}'")

    @staticmethod
    def normalise_language(language: str | None) -> str | None:
        """Validate a `lang` filter, or `None` when it was not supplied."""
        if language is None or not language.strip():
            return None
        value = language.strip().lower()
        if value not in SUPPORTED_LANGUAGES:
            raise ValueError(f"unsupported language '{language}'")
        return value

    # -- helpers -----------------------------------------------------------

    def _clamp_limit(self, limit: int | None) -> int:
        value = limit if isinstance(limit, int) and limit > 0 else self._settings.search_default_limit
        return max(1, min(value, self._settings.search_max_limit))

    @staticmethod
    def _favicon(url: str) -> str:
        host = (urlparse(url).hostname or "").lower()
        return f"https://{host}/favicon.ico" if host else ""

    @staticmethod
    def _as_markdown(query: str, results: list[dict[str, str]]) -> str:
        lines = [f"# {query}", ""]
        for index, result in enumerate(results, start=1):
            lines.append(f"{index}. **{result['title']}**")
            lines.append(f"   {result['url']}")
            if result["snippet"]:
                lines.append(f"   {result['snippet']}")
            lines.append("")
        return "\n".join(lines).strip() + "\n"


__all__ = [
    "CACHE_MAX_ENTRIES",
    "CACHE_TTL_SECONDS",
    "CATEGORIES",
    "SearchCache",
    "SOURCE_LABEL",
    "SUPPORTED_LANGUAGES",
    "SearchResult",
    "SearchService",
    "kind_of",
    "to_result",
    "youtube_thumbnail",
]
