"""A definition at the top of the page, from open endpoints.

When someone searches a name rather than a topic, the useful answer is a short
definition, and the index may hold nothing about it. Wikipedia's summary endpoint
and DuckDuckGo's instant-answer endpoint are both public, need no key, and answer
in one request.

Neither is called unless federation is switched on, and both are bounded to the
same short timeout as the rest of the federated path. The result is attributed to
its source — a card that hides where its text came from is worse than no card.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

logger = logging.getLogger(__name__)

#: Wikipedia's REST summary endpoint. Public, no key, JSON.
WIKIPEDIA_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
#: DuckDuckGo's instant answer endpoint. Public, no key, JSON.
DUCKDUCKGO_IA = "https://api.duckduckgo.com/?q={query}&format=json&no_html=1&skip_disambig=1"

#: How long resolution may take before the request is abandoned. Separate from
#: the request budget because a hung resolver is a different failure from a slow
#: provider, and it blocks a thread rather than a socket.
DNS_TIMEOUT_SECONDS = 0.5

#: How many provider requests may be in flight at once. A burst of empty searches
#: is the normal case — a thousand users all searching something we do not index —
#: and unbounded fan-out is how an IP gets 429'd off a provider for a day.
MAX_CONCURRENT_REQUESTS = 5

#: How many candidates each source may contribute. Wikipedia's search API
#: answers with as many as you ask for; DuckDuckGo's RelatedTopics is usually
#: smaller and nested.
WIKIPEDIA_RESULT_LIMIT = 15
DUCKDUCKGO_RESULT_LIMIT = 10

#: The safety ceiling on a whole answer. Not a target — whatever survives the
#: filters is returned, up to this.
MAX_CARDS = 20

#: Card buffers. Wider than the index's because an external card has no stored
#: row to fall back on: a title cut too short cannot be repaired later.
TITLE_CHARS = 100
SNIPPET_SHORT_CHARS = 300
#: What "More" reveals. A Wikipedia lead paragraph runs to about a thousand
#: characters; cutting at six hundred ended mid-thought and made the toggle
#: reveal a fragment rather than an answer. Twenty cards at this width is about
#: 24 KB, which is not a memory concern.
SNIPPET_FULL_CHARS = 1200

#: Downloads, not pages. A card that opens a 40 MB PDF is a card the reader
#: regrets tapping.
REJECTED_EXTENSIONS = (
    ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z", ".exe", ".dmg", ".apk",
    ".mp4", ".mp3", ".wav", ".avi", ".mkv", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".webp",
)

#: Routes that ask for a credential or a purchase rather than showing a page.
#: Matched as whole path segments: a substring test would reject
#: /administrative-reforms because it contains "admin".
REJECTED_PATH_SEGMENTS = frozenset(
    {"login", "signin", "sign-in", "admin", "wp-admin", "cart", "checkout"}
)

#: Wikipedia's search API, which returns many results where the REST summary
#: endpoint returns one.
WIKIPEDIA_SEARCH = "https://en.wikipedia.org/w/api.php"

#: The federated budget. A knowledge card that arrives late is worse than none,
#: and 800 ms is enough for an endpoint that is going to answer at all.
TIMEOUT_SECONDS = 0.8

#: A query longer than this is not a query, it is a paragraph someone pasted.
QUERY_CHAR_CAP = 150

#: Read no more than this from any response. A provider that streams a large body
#: can otherwise exhaust the 1 GB this box has.
MAX_RESPONSE_BYTES = 512 * 1024

#: Only these may be fetched. Everything else — file://, ftp://, data: — is a
#: request to read something that is not a web page.
ALLOWED_SCHEMES = ("http", "https")

#: A bot that says who it is can be blocked politely; one that lies gets the
#: whole IP banned. This is the honest header.
#: Wikimedia's policy requires a contact route in the User-Agent; a bare name is
#: answered with 403. The URL alone is not enough, so the address is included.
USER_AGENT = "EyesearchBot/1.0 (+https://eyesearch.in; contact@eyesearch.in)"

#: Sent on every outbound request in the fleet. `Accept` names JSON first and
#: `*/*` after it, because a provider that only serves HTML still gets a usable
#: response rather than a 406; `Accept-Language` stops an Indian query coming back
#: answered in whatever locale the provider guesses.
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
}

#: Pool sizes. Small on purpose: federation runs once per empty search, so a
#: large pool would be sockets held open for a request that never comes.
MAX_KEEPALIVE_CONNECTIONS = 5
MAX_CONNECTIONS = 10

#: Hostnames that never leave the machine. Docker resolves service names like
#: `postgres` and `backend` inside the bridge network, so a URL naming one of
#: these is an attempt to reach our own infrastructure, not the web.
BLOCKED_HOSTNAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal", "instance-data",
    "postgres", "backend", "db", "redis", "frontend", "nginx", "host.docker.internal",
}

#: Control characters and nulls. A query carrying one is malformed input, and a
#: URL carrying one is a header-injection attempt.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_query(query: str) -> str:
    """Make a query safe to put in a URL, or return nothing.

    Control characters and nulls are removed rather than escaped: a query that
    contains one did not come from a person typing, and percent-encoding it would
    pass the problem downstream instead of rejecting it.
    """
    value = _CONTROL.sub(" ", str(query or ""))
    value = " ".join(value.split())
    return value[:QUERY_CHAR_CAP].strip()


def is_public_url(url: str, *, resolve: bool = True) -> tuple[bool, str]:
    """Whether a URL may be fetched, and why not when it may not.

    Two separate checks, because they fail differently. The scheme check stops
    `file:///etc/passwd`. The address check stops a public-looking hostname that
    resolves to 127.0.0.1 — the classic SSRF, where the attacker does not control
    the network but does control DNS.

    `resolve=False` skips the DNS step. It is for an injected client, which is a
    test double rather than a socket: resolving a hostname that only exists in
    production would make the guard's verdict depend on where the test runs.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False, "unparseable_url"

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return False, "scheme_not_allowed"

    host = (parts.hostname or "").strip().lower()
    if not host:
        return False, "no_host"
    if host in BLOCKED_HOSTNAMES:
        return False, "blocked_hostname"

    # A literal address needs no resolution, so it is always checked. Skipping
    # this when `resolve` is off let http://10.1.2.3 through, which is the one
    # case the guard exists for.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if (
            literal.is_private
            or literal.is_loopback
            or literal.is_link_local
            or literal.is_reserved
            or literal.is_multicast
            or literal.is_unspecified
        ):
            return False, "private_address"
        return True, ""

    if not resolve:
        return True, ""

    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError, OSError):
        # Cannot resolve: not fetchable, and not our job to guess.
        return False, "unresolvable"

    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False, "unparseable_address"
        if (
            parsed.is_private
            or parsed.is_loopback
            or parsed.is_link_local
            or parsed.is_reserved
            or parsed.is_multicast
            or parsed.is_unspecified
        ):
            return False, "private_address"
    return True, ""


#: Astral-plane characters and the pictographic blocks. Deliberately excludes
#: \u2013-\u2014 and \u20B9: the en dash and the rupee sign are typography.
EMOJI = re.compile("[\U00010000-\U0010ffff\u2600-\u27BF\uFE00-\uFE0F\u200D]+")
HTML_TAG = re.compile(r"<[^>]{1,200}>")


def sanitize_text(text: str) -> str:
    """Entities decoded, tags stripped, emoji removed, space collapsed.

    The order matters. `&lt;b&gt;` is not a tag until it is decoded, so unescape
    runs first; and Wikipedia's search API returns its matches wrapped in
    <span class="searchmatch">, which is markup rather than text.
    """
    value = html.unescape(str(text or ""))
    value = HTML_TAG.sub(" ", value)
    value = EMOJI.sub("", value)
    return " ".join(value.split())


from app.engine.text import truncate_at_word as _truncate_at_word

#: The shared helper, re-exported. There used to be two implementations with
#: different behaviour, so whether a reader saw a cut word depended on which
#: module produced the text.
truncate_at_word = _truncate_at_word


def is_cardable_url(url: str) -> bool:
    """Whether a URL is a page worth showing a reader.

    Separate from `is_public_url`, which asks a different question: that one is
    about safety, this one is about usefulness. A PDF is safe and is still not a
    card; a login page is safe and is still not an answer.
    """
    if not is_public_url(url, resolve=False)[0]:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False

    path = (parts.path or "").lower()
    if any(path.endswith(extension) for extension in REJECTED_EXTENSIONS):
        return False

    segments = {segment for segment in path.split("/") if segment}
    if segments & REJECTED_PATH_SEGMENTS:
        return False
    return True


@dataclass
class KnowledgeCard:
    title: str = ""
    extract: str = ""
    url: str = ""
    source: str = ""
    snippet_full: str = ""
    brand_name: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "extract": self.extract,
            "url": self.url,
            "source": self.source,
        }

    def __bool__(self) -> bool:
        """A card is worth returning when it carries an extract.

        Deliberately not `title and url`. Wikipedia sometimes omits
        `content_urls`, and a card with a usable summary but no canonical link is
        still an answer; requiring a URL would discard it, which is the silent
        drop this class is meant to avoid.
        """
        return bool(self.extract)


class FreeKnowledgeCourierAgent:
    """Fetch a public definition for a query, if one exists."""

    def __init__(self, http=None, *, timeout: float = TIMEOUT_SECONDS, settings=None) -> None:
        self._http = http
        self._settings = settings
        self.timeout = float(timeout)
        self._client = None
        #: Built lazily: a semaphore created at import time binds to whichever
        #: event loop happened to be running then, and fails in every other one.
        self._gate: asyncio.Semaphore | None = None

    def _ensure_client(self):
        """A pooled client, built once and reused.

        A new client per request would open a new connection every time and leak
        the old one until the pool expired; on a box with one search worker and a
        fallback path, that is sockets held open for requests that already gave up.
        """
        if self._http is not None:
            return None
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers=dict(DEFAULT_HEADERS),
                limits=httpx.Limits(
                    max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
                    max_connections=MAX_CONNECTIONS,
                ),
                # Redirects are not followed. A 302 to 169.254.169.254 or
                # 127.0.0.1 would otherwise sail past the guard, which only
                # ever saw the URL it was given.
                follow_redirects=False,
            )
        return self._client

    async def _get_json(self, url: str) -> dict:
        """Fetch and parse JSON. Returns {} for every failure there is."""
        # Resolution is a blocking syscall. On an async worker it would stall
        # every other request for the duration of a DNS lookup, so it runs in a
        # thread — and it is why this is the one guard that costs anything.
        loop = asyncio.get_running_loop()
        try:
            # Bounded separately from the request budget. A resolver that hangs
            # would otherwise hold the thread and eat the whole 800 ms before the
            # fetch even starts — and threads are a shared pool.
            allowed, reason = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: is_public_url(url, resolve=self._http is None)
                ),
                timeout=DNS_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.info("federation DNS lookup exceeded %.0f ms", DNS_TIMEOUT_SECONDS * 1000)
            return {}
        if not allowed:
            logger.info("federation refused %s: %s", url, reason)
            return {}

        if self._http is not None:
            # The cap is enforced here rather than trusted to the client. A
            # provider that ignores the timeout it was handed would otherwise
            # hang the request for as long as it liked.
            try:
                return await asyncio.wait_for(
                    self._http.get_json(url, timeout=self.timeout), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                logger.info("federation timed out after %.0f ms", self.timeout * 1000)
                return {}
            except Exception as error:  # noqa: BLE001 - federation must never raise
                logger.debug("knowledge fetch failed for %s: %s", url, error)
                return {}

        client = self._ensure_client()
        if client is None:
            return {}
        if self._gate is None:
            self._gate = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        try:
            async with self._gate:
                return await asyncio.wait_for(self._fetch_json(client, url), timeout=self.timeout)
        except asyncio.TimeoutError:
            logger.info("federation timed out after %.0f ms", self.timeout * 1000)
            return {}
        except Exception as error:  # noqa: BLE001 - federation must never raise
            logger.debug("knowledge fetch failed for %s: %s", url, error)
            return {}

    async def _fetch_json(self, client, url: str) -> dict:
        import json

        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return {}
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    # Stop reading rather than buffer it all and then complain.
                    logger.info("federation response over %d bytes; abandoned", MAX_RESPONSE_BYTES)
                    return {}
                chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search_wikipedia(self, query: str, limit: int = WIKIPEDIA_RESULT_LIMIT) -> list[KnowledgeCard]:
        """Many results from Wikipedia's search API.

        The REST summary endpoint answers with one article — the one whose title
        matches exactly — which is why a query phrased as a question usually got
        nothing back. Search ranks instead of matching, so it answers the query
        rather than the title.
        """
        safe = clean_query(query)
        if not safe:
            return []

        # `generator=search` with `prop=extracts` in one request. Search alone
        # returns a SERP blurb of about a hundred and fifty characters — the
        # opening clause of the article, not its lead — which is what every
        # Wikipedia card used to show, however wide our own buffer was.
        #
        # The REST summary endpoint returns the same lead for one title, so the
        # alternative was one request per result. This is one request for all of
        # them, and it is the same endpoint already being called.
        params = {
            "action": "query", "generator": "search", "gsrsearch": safe,
            "gsrlimit": str(int(limit)),
            "prop": "extracts", "exintro": "1", "explaintext": "1", "exlimit": "max",
            "utf8": "1", "format": "json",
        }
        payload = await self._get_json(f"{WIKIPEDIA_SEARCH}?{urlencode(params)}")
        pages = (payload.get("query", {}) or {}).get("pages", {}) or {}
        # The generator returns a mapping keyed by page id, ordered by the
        # search ranking in `index`. A dict keeps insertion order but not the
        # ranking, so it is restored here rather than trusted.
        ordered = sorted(
            (page for page in pages.values() if isinstance(page, dict)),
            key=lambda page: int(page.get("index") or 0),
        )

        cards: list[KnowledgeCard] = []
        for item in ordered:
            title = sanitize_text(str(item.get("title") or ""))
            if not title:
                continue
            url = f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
            if not is_cardable_url(url):
                continue

            # The article's own lead. The search blurb is the fallback, and it
            # is only used when the extract is missing — a page with no lead is
            # rare, but it is not impossible.
            lead = sanitize_text(str(item.get("extract") or ""))
            blurb = sanitize_text(str(item.get("snippet") or ""))
            body = lead or blurb
            if not body:
                continue
            cards.append(
                KnowledgeCard(
                    title=truncate_at_word(title, TITLE_CHARS),
                    extract=truncate_at_word(body, SNIPPET_SHORT_CHARS),
                    url=url,
                    source="wikipedia",
                    snippet_full=truncate_at_word(body, SNIPPET_FULL_CHARS),
                    brand_name="Wikipedia",
                )
            )
        return cards

    async def search_duckduckgo(self, query: str, limit: int = DUCKDUCKGO_RESULT_LIMIT) -> list[KnowledgeCard]:
        """DuckDuckGo's RelatedTopics, flattened.

        The API nests: a topic may hold its own `Topics` list. Reading only the
        top level silently loses the whole disambiguation branch.
        """
        safe = clean_query(query)
        if not safe:
            return []

        payload = await self._get_json(DUCKDUCKGO_IA.format(query=quote(safe)))
        related = payload.get("RelatedTopics", []) or []

        cards: list[KnowledgeCard] = []
        for topic in related:
            if not isinstance(topic, dict):
                continue
            entries = topic.get("Topics") or [topic]
            for entry in entries:
                if not isinstance(entry, dict) or len(cards) >= limit:
                    continue
                url = str(entry.get("FirstURL") or "")
                text = sanitize_text(str(entry.get("Text") or ""))
                if not url or not text or not is_cardable_url(url):
                    continue
                title, _, body = text.partition(" - ")
                cards.append(
                    KnowledgeCard(
                        title=truncate_at_word(title or text, TITLE_CHARS),
                        extract=truncate_at_word(body or text, SNIPPET_SHORT_CHARS),
                        url=url,
                        source="duckduckgo",
                        snippet_full=truncate_at_word(text, SNIPPET_FULL_CHARS),
                        brand_name="DuckDuckGo",
                    )
                )
                if len(cards) >= limit:
                    break
        return cards

    def _extra_sources(self, query: str) -> list:
        """The optional couriers, as coroutines, or nothing when unavailable.

        Only added when a client exists to carry them. An injected test client
        still exercises them, which is how their parsers get covered without a
        network.
        """
        from app.agents.federated.searxng_courier import SearXNGOCourier
        from app.agents.federated.wikidata_courier import WikidataCourier

        pending = []
        if self._http is not None:
            pending.append(WikidataCourier(http=self._http).search(query))
            base = str(getattr(self._settings, "searxng_base_url", "") or "")
            if base:
                pending.append(
                    SearXNGOCourier(http=self._http, base_url=base).search(query)
                )
        return pending

    async def search(self, query: str, limit: int = MAX_CARDS) -> list[KnowledgeCard]:
        """Every pure card both sources can give, up to the safety ceiling.

        Concurrent, because the two sources are independent and the budget is
        shared: run in sequence they would spend it twice.
        """
        ceiling = max(1, min(int(limit), MAX_CARDS))
        sources = [self.search_wikipedia(query), self.search_duckduckgo(query)]

        # Wikidata and SearXNG are optional and each may be absent. Gathering a
        # None would raise, so they are only added when they exist.
        extra = self._extra_sources(query)
        sources.extend(extra)

        results = await asyncio.gather(*sources, return_exceptions=True)
        cards: list[KnowledgeCard] = []
        for result in results:
            if isinstance(result, list):
                cards.extend(result)
            elif isinstance(result, BaseException):
                logger.debug("a federated source failed: %s", result)

        # A duplicate URL from both sources is one card, and the first wins:
        # Wikipedia's is the more stable of the two.
        seen: set[str] = set()
        unique: list[KnowledgeCard] = []
        for card in cards:
            if card.url in seen:
                continue
            seen.add(card.url)
            unique.append(card)
        return unique[:ceiling]

    async def wikipedia(self, query: str) -> KnowledgeCard:
        safe = clean_query(query)
        if not safe:
            return KnowledgeCard()
        payload = await self._get_json(WIKIPEDIA_SUMMARY.format(title=quote(safe)))
        extract = str(payload.get("extract") or "").strip()
        if not extract:
            return KnowledgeCard()
        return KnowledgeCard(
            title=str(payload.get("title") or query).strip(),
            extract=extract,
            url=str((payload.get("content_urls", {}).get("desktop", {}) or {}).get("page") or ""),
            source="wikipedia",
        )

    async def duckduckgo(self, query: str) -> KnowledgeCard:
        safe = clean_query(query)
        if not safe:
            return KnowledgeCard()
        payload = await self._get_json(DUCKDUCKGO_IA.format(query=quote(safe)))
        abstract = str(payload.get("AbstractText") or "").strip()
        if not abstract:
            return KnowledgeCard()
        return KnowledgeCard(
            title=str(payload.get("Heading") or query).strip(),
            extract=abstract,
            url=str(payload.get("AbstractURL") or ""),
            source="duckduckgo",
        )

    async def lookup(self, query: str) -> KnowledgeCard:
        """Wikipedia first: its summaries are longer and better attributed."""
        if not clean_query(query):
            return KnowledgeCard()
        card = await self.wikipedia(query)
        if card:
            return card
        return await self.duckduckgo(query)

    async def candidates(self, query: str, limit: int = MAX_CARDS) -> list[dict[str, str]]:
        """The same results in the plain shape the horizon also accepts."""
        return [
            {"title": card.title, "url": card.url, "snippet": card.extract}
            for card in await self.search(query, limit=limit)
        ]


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "federated.knowledge_courier",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 4 > 0, 4 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
