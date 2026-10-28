"""Sitemap discovery, the in-memory search cache, and snippet highlighting."""

from __future__ import annotations

import asyncio

import httpx

from app.agents import load_agent
from app.engine.store import PageDocument, highlight_terms
from app.orchestrator import Orchestrator
from app.services.search_service import SearchCache, SearchService

frontier_module = load_agent("01_frontier_agent")

FrontierAgent = frontier_module.FrontierAgent
discover_sitemap_urls = frontier_module.discover_sitemap_urls
parse_sitemap_locs = frontier_module.parse_sitemap_locs
SITEMAP_MAX_URLS_PER_DOMAIN = frontier_module.SITEMAP_MAX_URLS_PER_DOMAIN
SITEMAP_COOLDOWN_SECONDS = frontier_module.SITEMAP_COOLDOWN_SECONDS
FRONTIER_LOW_WATER = frontier_module.FRONTIER_LOW_WATER

BODY = (
    "The department publishes notifications, citizen services and contact details "
    "for district offices across the state of Karnataka."
)


def _doc(url: str, title: str, body: str = BODY, digest: str = "") -> PageDocument:
    return PageDocument(
        url=url,
        domain=url.split("//", 1)[1].split("/", 1)[0],
        title=title,
        description=f"{title} description.",
        body_text=body,
        digest=digest or url,
    )


# ---------------------------------------------------------------------------
# Sitemaps
# ---------------------------------------------------------------------------

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://karnataka.gov.in/about</loc></url>
  <url><loc>https://karnataka.gov.in/departments/health</loc></url>
  <url><loc>https://karnataka.gov.in/logo.png</loc></url>
  <url><loc>https://karnataka.gov.in/login</loc></url>
  <url><loc>https://elsewhere.xyz/page</loc></url>
</urlset>"""

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://karnataka.gov.in/sitemap-pages.xml</loc></sitemap>
</sitemapindex>"""


def test_locs_are_extracted_from_a_sitemap():
    assert parse_sitemap_locs(SITEMAP) == [
        "https://karnataka.gov.in/about",
        "https://karnataka.gov.in/departments/health",
        "https://karnataka.gov.in/logo.png",
        "https://karnataka.gov.in/login",
        "https://elsewhere.xyz/page",
    ]


def test_an_empty_or_malformed_sitemap_yields_nothing():
    assert parse_sitemap_locs("") == []
    assert parse_sitemap_locs("<html><body>not a sitemap</body></html>") == []


async def test_discovery_filters_to_crawlable_in_scope_urls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SITEMAP)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        urls = await discover_sitemap_urls("https://karnataka.gov.in/", client)
    finally:
        await client.aclose()

    assert urls == [
        "https://karnataka.gov.in/about",
        "https://karnataka.gov.in/departments/health",
    ]


async def test_discovery_is_capped_per_domain():
    many = "".join(f"<url><loc>https://karnataka.gov.in/p{i}</loc></url>" for i in range(500))
    document = f"<urlset>{many}</urlset>"

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=document))
    )
    try:
        urls = await discover_sitemap_urls("https://karnataka.gov.in/", client)
    finally:
        await client.aclose()

    assert len(urls) == SITEMAP_MAX_URLS_PER_DOMAIN


async def test_a_sitemap_index_is_followed_one_level():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("sitemap-pages.xml"):
            return httpx.Response(200, text=SITEMAP)
        return httpx.Response(200, text=INDEX)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        urls = await discover_sitemap_urls("https://karnataka.gov.in/", client)
    finally:
        await client.aclose()

    assert "https://karnataka.gov.in/about" in urls


async def test_a_missing_sitemap_yields_nothing_and_does_not_raise():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, text="not found"))
    )
    try:
        assert await discover_sitemap_urls("https://karnataka.gov.in/", client) == []
    finally:
        await client.aclose()


async def test_a_network_failure_yields_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await discover_sitemap_urls("https://karnataka.gov.in/", client) == []
    finally:
        await client.aclose()


async def test_discovery_without_a_client_is_a_no_op():
    assert await discover_sitemap_urls("https://karnataka.gov.in/", None) == []


async def test_harvesting_queues_sitemap_urls(store, client, settings):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=SITEMAP))
    )
    frontier = FrontierAgent(store, settings, client=http)
    try:
        added = await frontier.harvest_sitemap(domains=("karnataka.gov.in",))

        assert added == 2
        assert set(await store.pending_domains()) == {"karnataka.gov.in"}
    finally:
        await http.aclose()


async def test_a_domain_is_not_harvested_twice_within_the_cooldown(store, client, settings):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=SITEMAP)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    frontier = FrontierAgent(store, settings, client=http)
    try:
        await frontier.harvest_sitemap(domains=("karnataka.gov.in",))
        await frontier.harvest_sitemap(domains=("karnataka.gov.in",))
    finally:
        await http.aclose()

    assert len(calls) == 1
    assert SITEMAP_COOLDOWN_SECONDS > 0


async def test_harvesting_is_skipped_when_the_frontier_is_healthy(store, client, settings):
    await store.enqueue_urls(
        [(f"https://busy.gov.in/p{i}", "busy.gov.in") for i in range(FRONTIER_LOW_WATER)]
    )
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=SITEMAP))
    )
    frontier = FrontierAgent(store, settings, client=http)
    try:
        assert await frontier.harvest_sitemap(domains=("karnataka.gov.in",), force=False) == 0
        # A full queue must not block a domain we were explicitly asked about.
        assert await frontier.harvest_sitemap(domains=("karnataka.gov.in",), force=True) == 2
    finally:
        await http.aclose()


# ---------------------------------------------------------------------------
# Search cache
# ---------------------------------------------------------------------------


class _CountingStore:
    """Wraps a store and counts searches, to prove a cache hit skips the DB."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.searches = 0

    async def search(self, *args, **kwargs):
        self.searches += 1
        return await self._inner.search(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def test_a_second_identical_search_is_served_from_the_cache(store):
    await store.persist_batch([_doc("https://karnataka.gov.in/metro", "Metro timings")])
    counting = _CountingStore(store)
    service = SearchService(counting)

    first = await service.search("metro")
    second = await service.search("metro")

    assert counting.searches == 1
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert second["took_ms"] == 0
    assert first["results"] == second["results"]


async def test_a_different_filter_is_a_cache_miss(store):
    await store.persist_batch([_doc("https://karnataka.gov.in/metro", "Metro timings")])
    counting = _CountingStore(store)
    service = SearchService(counting)

    await service.search("metro")
    await service.search("metro", category="government")

    assert counting.searches == 2


async def test_a_different_page_is_a_cache_miss(store):
    await store.persist_batch([_doc("https://karnataka.gov.in/metro", "Metro timings")])
    counting = _CountingStore(store)
    service = SearchService(counting)

    await service.search("metro", page=1)
    await service.search("metro", page=2)

    assert counting.searches == 2


async def test_highlighting_is_part_of_the_cache_key(store):
    await store.persist_batch([_doc("https://karnataka.gov.in/metro", "Metro timings")])
    counting = _CountingStore(store)
    service = SearchService(counting)

    await service.search("metro", highlight=False)
    await service.search("metro", highlight=True)

    assert counting.searches == 2


async def test_the_flat_contract_is_cached_too(store):
    await store.persist_batch([_doc("https://karnataka.gov.in/metro", "Metro timings")])
    counting = _CountingStore(store)
    service = SearchService(counting)

    await service.search_flat("metro")
    second = await service.search_flat("metro")

    assert counting.searches == 1
    assert second["cache_hit"] is True


def test_entries_expire_after_the_ttl():
    now = [0.0]
    cache = SearchCache(ttl_seconds=120.0, clock=lambda: now[0])
    cache.set(("k",), {"value": 1})

    now[0] = 119.0
    assert cache.get(("k",)) is not None

    now[0] = 121.0
    assert cache.get(("k",)) is None


def test_the_cache_evicts_the_oldest_entry_at_capacity():
    cache = SearchCache(max_entries=2)

    cache.set(("a",), {"v": "a"})
    cache.set(("b",), {"v": "b"})
    cache.set(("c",), {"v": "c"})

    assert cache.get(("a",)) is None
    assert cache.get(("c",)) is not None
    assert len(cache) == 2


def test_a_cached_payload_cannot_be_mutated_by_a_caller():
    cache = SearchCache()
    cache.set(("k",), {"results": [{"title": "original"}]})

    fetched = cache.get(("k",))
    fetched["results"][0]["title"] = "tampered"

    assert cache.get(("k",))["results"][0]["title"] == "original"


def test_hits_and_misses_are_counted():
    cache = SearchCache()
    cache.set(("k",), {"v": 1})

    cache.get(("k",))
    cache.get(("missing",))

    assert cache.hits == 1
    assert cache.misses == 1


# ---------------------------------------------------------------------------
# Highlighting
# ---------------------------------------------------------------------------


def test_query_terms_are_wrapped_and_casing_is_preserved():
    result = highlight_terms("Metro fares were revised by METRO", ["metro"])

    assert result == "<b>Metro</b> fares were revised by <b>METRO</b>"


def test_untrusted_markup_is_escaped():
    result = highlight_terms("Use <script>alert(1)</script> & metro", ["metro"])

    assert "&lt;script&gt;" in result
    assert "<script>" not in result
    assert "&amp;" in result
    assert result.endswith("<b>metro</b>")


def test_only_whole_words_are_wrapped():
    result = highlight_terms("metroplex and metro", ["metro"])

    assert result == "metroplex and <b>metro</b>"


def test_no_tokens_leaves_the_text_escaped_but_unmarked():
    assert highlight_terms("plain text", []) == "plain text"


async def test_snippets_carry_highlighting_only_when_asked(store):
    await store.persist_batch(
        [_doc("https://karnataka.gov.in/metro", "Metro timings", BODY + " metro fares")]
    )
    service = SearchService(store)

    plain = await service.search("metro")
    marked = await service.search("metro", highlight=True)

    assert "<b>" not in plain["results"][0]["snippet"]
    assert "<b>" in marked["results"][0]["snippet"]


async def test_forcing_a_harvest_still_respects_the_cooldown(store, client, settings):
    """`force` bypasses the depth gate, not the once-an-hour rule."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=SITEMAP)

    await store.enqueue_urls(
        [(f"https://busy.gov.in/p{i}", "busy.gov.in") for i in range(FRONTIER_LOW_WATER)]
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    frontier = FrontierAgent(store, settings, client=http)
    try:
        first = await frontier.harvest_sitemap(domains=("karnataka.gov.in",), force=True)
        second = await frontier.harvest_sitemap(domains=("karnataka.gov.in",), force=True)
    finally:
        await http.aclose()

    assert first == 2
    assert second == 0
    assert len(calls) == 1


async def test_the_orchestrator_harvests_for_a_newly_discovered_domain(store, client, settings):
    """Discovery of an authoritative domain triggers its sitemap immediately."""
    page = (
        "<html><head><title>Karnataka portal</title></head><body>"
        '<a href="https://www.thehindu.com/news/">news</a>'
        "<p>The department publishes notifications, citizen services and contact "
        "details for district offices across the state of Karnataka.</p></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("sitemap.xml"):
            return httpx.Response(
                200,
                text="<urlset><url><loc>https://www.thehindu.com/news/national</loc></url></urlset>",
            )
        return httpx.Response(200, text=page)

    orchestrator = Orchestrator(client, settings, transport=httpx.MockTransport(handler))
    try:
        await orchestrator.frontier.enqueue(["https://karnataka.gov.in/"])
        outcome = await orchestrator.run_batch()

        # Harvesting is scheduled, not awaited, so page indexing never waits on
        # a sitemap fetch. The batch reports the domains it handed over.
        assert outcome.discovered >= 1
        assert outcome.sitemap_domains_scheduled >= 1

        # The background drain is where the sitemap is actually read; wait for
        # it and check the URLs really reached the queue.
        task = orchestrator._sitemap_harvest_task
        assert task is not None
        await asyncio.wait_for(task, timeout=5)

        queued = await client.execute(
            "SELECT url FROM crawl_queue WHERE status = 'pending'"
        )
        urls = [str(row[0]) for row in queued.rows]
        assert any("thehindu.com/news/national" in url for url in urls), urls
    finally:
        await orchestrator.close()
