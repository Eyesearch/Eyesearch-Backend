"""robots.txt: parsing, the cache, and the fail-open policy."""

from __future__ import annotations

import httpx

from app.agents import load_agent
from app.engine.config import Settings
from app.orchestrator import Orchestrator

robots_module = load_agent("09_robots_agent")
RobotsCache = robots_module.RobotsCache
parse_robots = robots_module.parse_robots

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0.0 Safari/537.36"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_a_disallowed_prefix_is_refused():
    rules = parse_robots("User-agent: *\nDisallow: /private\n", UA)

    assert rules.allows("/private/page") is False
    assert rules.allows("/public") is True


def test_a_longer_allow_overrides_a_shorter_disallow():
    rules = parse_robots(
        "User-agent: *\nDisallow: /private\nAllow: /private/public\n", UA
    )

    assert rules.allows("/private/secret") is False
    assert rules.allows("/private/public/notice") is True


def test_an_empty_disallow_allows_everything():
    rules = parse_robots("User-agent: *\nDisallow:\n", UA)

    assert rules.allows("/anything") is True


def test_wildcards_and_end_anchors_are_honoured():
    rules = parse_robots("User-agent: *\nDisallow: /*.pdf$\n", UA)

    assert rules.allows("/docs/report.pdf") is False
    assert rules.allows("/docs/report.pdfx") is True


def test_a_named_agent_group_is_ignored_for_a_browser_ua():
    """Our UA is a desktop browser, so only the `*` group applies."""
    document = (
        "User-agent: Googlebot\n"
        "Disallow: /\n"
        "\n"
        "User-agent: *\n"
        "Disallow: /admin\n"
    )
    rules = parse_robots(document, UA)

    assert rules.allows("/") is True
    assert rules.allows("/admin") is False


def test_an_empty_document_allows_everything():
    assert parse_robots("", UA).allows("/x") is True
    assert parse_robots("garbage without colons", UA).allows("/x") is True


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


def _client(handler, **kwargs):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


async def test_rules_are_cached_per_host():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")

    http = _client(handler)
    cache = RobotsCache(http, user_agent=UA)
    try:
        assert await cache.allowed("https://a.gov.in/private/x") is False
        assert await cache.allowed("https://a.gov.in/public") is True
        assert await cache.allowed("https://a.gov.in/other") is True
    finally:
        await http.aclose()

    assert calls["n"] == 1, "one fetch per host, not per URL"


async def test_a_missing_robots_file_allows_the_crawl():
    http = _client(lambda request: httpx.Response(404))
    cache = RobotsCache(http, user_agent=UA)
    try:
        assert await cache.allowed("https://a.gov.in/x") is True
    finally:
        await http.aclose()


async def test_a_server_error_allows_the_crawl():
    http = _client(lambda request: httpx.Response(503))
    cache = RobotsCache(http, user_agent=UA)
    try:
        assert await cache.allowed("https://a.gov.in/x") is True
    finally:
        await http.aclose()


async def test_a_transport_failure_allows_the_crawl():
    def handler(request):
        raise httpx.ConnectTimeout("no route")

    http = _client(handler)
    cache = RobotsCache(http, user_agent=UA)
    try:
        assert await cache.allowed("https://a.gov.in/x") is True
    finally:
        await http.aclose()

    assert cache.failures == 1


async def test_the_cache_is_bounded():
    def handler(request):
        return httpx.Response(200, text="User-agent: *\nDisallow: /x\n")

    http = _client(handler)
    cache = RobotsCache(http, user_agent=UA, max_entries=3)
    try:
        for index in range(10):
            await cache.allowed(f"https://site{index}.gov.in/page")
    finally:
        await http.aclose()

    assert len(cache) == 3


async def test_a_failed_lookup_is_cached_briefly_rather_than_refetched():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectTimeout("no route")

    http = _client(handler)
    cache = RobotsCache(http, user_agent=UA)
    try:
        await cache.allowed("https://a.gov.in/1")
        await cache.allowed("https://a.gov.in/2")
    finally:
        await http.aclose()

    assert calls["n"] == 1


async def test_concurrent_callers_share_one_fetch():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")

    http = _client(handler)
    cache = RobotsCache(http, user_agent=UA)
    try:
        import asyncio

        results = await asyncio.gather(
            *[cache.allowed(f"https://a.gov.in/p{i}") for i in range(10)]
        )
    finally:
        await http.aclose()

    assert calls["n"] == 1
    assert all(results)


# ---------------------------------------------------------------------------
# The crawl loop honours it
# ---------------------------------------------------------------------------


async def test_the_orchestrator_parks_a_disallowed_url(store, client):
    body = (
        "<html><head><title>Portal</title></head><body><p>"
        "The department publishes notifications, citizen services and contact "
        "details for district offices across the state.</p></body></html>"
    )

    def handler(request):
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /blocked\n")
        return httpx.Response(200, text=body)

    settings = Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
    )
    orchestrator = Orchestrator(client, settings, transport=httpx.MockTransport(handler))
    try:
        await orchestrator.frontier.enqueue(["https://karnataka.gov.in/blocked/page"])
        outcome = await orchestrator.run_batch()

        assert outcome.attempted == 1
        assert outcome.indexed_pages == 0
        assert outcome.skip_reasons.get("robots_disallow") == 1
    finally:
        await orchestrator.close()
