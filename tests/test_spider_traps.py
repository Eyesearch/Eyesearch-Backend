"""Universal spider traps: error pages, auth walls, facets, recursion, redirects."""

from __future__ import annotations

import httpx
import pytest

from app.agents import load_agent
from app.engine.config import Settings
from app.orchestrator import Orchestrator

frontier = load_agent("01_frontier_agent")
fetcher_module = load_agent("02_fetcher_agent")
is_crawlable = frontier.is_crawlable
FetcherAgent = fetcher_module.FetcherAgent


# ---------------------------------------------------------------------------
# Admission: traps must never enter the frontier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # Government ASP.NET error pages and redirect traps.
        "https://udyamregistration.gov.in/udyam/ErrorPages/FileNotFound.aspx",
        "https://rbi.org.in/Scripts/Error.aspx?aspxerrorpath=/test",
        "https://x.gov.in/commonerrorpage.aspx",
        "https://x.gov.in/servererror.htm",
        # Private-site cart and auth walls.
        "https://paiinternational.in/checkout/cart",
        "https://example.com/login?redirect=https://example.com/login",
        "https://example.com/signin",
        "https://example.com/my-account/orders",
        # Directory recursion.
        "https://example.in/category/books/books/books/item",
        # Faceted search.
        "https://shop.in/items?a=1&b=2&c=3&d=4&e=5",
        # A URL concatenated onto another.
        "https://x.gov.in/a?b=1?c=2",
        # ASP.NET error bounces carry the marker in the query string.
        "https://x.gov.in/page.html?aspxerrorpath=/x",
        # Infinite calendar navigation.
        "https://x.gov.in/calendar/2026/10/28/",
    ],
)
def test_traps_are_rejected(url):
    assert is_crawlable(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://indiaai.gov.in/news/latest-ai-updates",
        "https://example.in/about-us",
        "https://karnataka.gov.in/en/departments/health/services",
        "https://x.gov.in/doc?id=5&lang=en",
        # `/cart` must not swallow `/cartography`.
        "https://example.in/cartography",
        # Nor `/login` swallow `/login-help`.
        "https://example.in/login-help",
        # A bare calendar page is a page; only the dated children are a trap.
        "https://example.in/calendar",
    ],
)
def test_real_pages_survive(url):
    assert is_crawlable(url) is True


def test_an_overlong_url_is_rejected():
    long_url = "https://x.gov.in/" + "a" * frontier.MAX_URL_LENGTH
    assert is_crawlable(long_url) is False


def test_tracking_params_are_stripped_before_the_facet_check():
    """A URL that is only long because of tracking params is still a page."""
    assert is_crawlable("https://x.gov.in/notice?utm_source=a&utm_medium=b") is True


# ---------------------------------------------------------------------------
# Fetch: a redirect that lands on a trap
# ---------------------------------------------------------------------------


def _agent(handler, **kwargs):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True, max_redirects=5
    )
    return FetcherAgent(Settings(environment="test"), client=client, **kwargs)


async def test_a_redirect_to_a_login_wall_is_rejected():
    def handler(request):
        if "redirect-me" in str(request.url):
            return httpx.Response(302, headers={"location": "https://x.gov.in/login?next=/a"})
        return httpx.Response(200, text="<html><body><p>" + "x" * 300 + "</p></body></html>")

    agent = _agent(handler)
    try:
        result = await agent.fetch("https://x.gov.in/redirect-me")
    finally:
        await agent.aclose()

    assert result.ok is False
    assert result.error == "rejected_soft_404_or_auth_trap"


async def test_a_redirect_to_an_error_page_is_rejected():
    def handler(request):
        if "old-page" in str(request.url):
            return httpx.Response(
                302, headers={"location": "https://x.gov.in/ErrorPages/FileNotFound.aspx"}
            )
        # The error page itself answers 200 — a soft 404.
        return httpx.Response(200, text="<html><body><p>Sorry, not found.</p></body></html>")

    agent = _agent(handler)
    try:
        result = await agent.fetch("https://x.gov.in/old-page")
    finally:
        await agent.aclose()

    assert result.ok is False
    assert result.error == "rejected_soft_404_or_auth_trap"


async def test_an_infinite_redirect_loop_is_bounded():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://x.gov.in/loop"})

    agent = _agent(handler)
    try:
        result = await agent.fetch("https://x.gov.in/loop")
    finally:
        await agent.aclose()

    assert result.ok is False
    assert "TooManyRedirects" in result.error


async def test_a_normal_redirect_still_works():
    def handler(request):
        if "old" in str(request.url):
            return httpx.Response(302, headers={"location": "https://x.gov.in/new"})
        return httpx.Response(200, text="<html><body><p>" + "x" * 300 + "</p></body></html>")

    agent = _agent(handler)
    try:
        result = await agent.fetch("https://x.gov.in/old")
    finally:
        await agent.aclose()

    assert result.ok is True


# ---------------------------------------------------------------------------
# Dirty hrefs: parts.port raises lazily
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # The exact shape from the Render log.
        "http://example.com:\xa0https://other.com",
        "https://domain.in:invalid_port/path",
        "http://test.in:8080foo/test",
    ],
)
def test_an_uncastable_port_returns_empty_instead_of_raising(url):
    """`parts.port` raises ValueError on access, not at parse time."""
    assert frontier.normalise_url(url) == ""


def test_a_trailing_space_after_the_colon_is_repairable():
    """`strip()` removes it, so the port is simply absent rather than invalid."""
    assert frontier.normalise_url("http://x.in: ") == "http://x.in/"


def test_a_real_port_is_preserved():
    assert frontier.normalise_url("http://test.in:8080/a") == "http://test.in:8080/a"


def test_the_default_ports_are_dropped():
    assert frontier.normalise_url("https://test.in:443/a") == "https://test.in/a"
    assert frontier.normalise_url("http://test.in:80/a") == "http://test.in/a"


async def test_one_dirty_link_does_not_cost_the_others(store):
    from app.engine.store import IndexStore

    agent = frontier.FrontierAgent(IndexStore(store, Settings(environment="test")))
    links = [
        "https://karnataka.gov.in/notice",
        "http://example.com:\xa0https://other.com",
        "https://iisc.ac.in/admissions",
    ]

    accepted = agent.filter_links(links, "https://karnataka.gov.in/")

    assert "https://karnataka.gov.in/notice" in accepted
    assert "https://iisc.ac.in/admissions" in accepted
    assert len(accepted) == 2


async def test_a_page_with_a_dirty_href_still_indexes(store, client):
    body = (
        "<html><head><title>District portal</title></head><body>"
        '<a href="http://example.com:\xa0https://other.com">broken</a>'
        '<a href="/notices">notices</a>'
        "<p>The department publishes notifications, citizen services and contact "
        "details for district offices across the state.</p></body></html>"
    )

    def handler(request):
        return httpx.Response(200, text=body)

    settings = Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
        robots_enabled=False,
    )
    orchestrator = Orchestrator(client, settings, transport=httpx.MockTransport(handler))
    try:
        await orchestrator.frontier.enqueue(["https://karnataka.gov.in/"])
        outcome = await orchestrator.run_batch()

        assert outcome.indexed_pages == 1, "the dirty href must not abort the batch"
        assert outcome.discovered >= 1, "the good link must still be discovered"
    finally:
        await orchestrator.close()


# ---------------------------------------------------------------------------
# Dirty hrefs: urljoin raises on an unclosed IPv6 literal
# ---------------------------------------------------------------------------


def test_urljoin_survives_an_unclosed_ipv6_href():
    """`urljoin(base, 'http://[::1/x')` raises ValueError: Invalid IPv6 URL."""
    parser = load_agent("03_parser_agent").ParserAgent
    html = (
        "<html><head><title>District portal</title></head><body>"
        '<a href="http://[::1/x">broken ipv6</a>'
        '<a href="/notices">notices</a>'
        '<a href="https://iisc.ac.in/admissions">admissions</a>'
        "<p>The department publishes notifications, citizen services and contact "
        "details for district offices across the state.</p></body></html>"
    )

    page = parser().parse("https://karnataka.gov.in/", html)

    assert page.ok is True
    assert page.title == "District portal"
    assert page.links == [
        "https://karnataka.gov.in/notices",
        "https://iisc.ac.in/admissions",
    ]


def test_a_bracket_in_a_query_is_not_dropped():
    """The pre-check `'[' in href` would discard a legitimate bracketed query."""
    parser = load_agent("03_parser_agent").ParserAgent
    html = (
        "<html><head><title>Search</title></head><body>"
        '<a href="/filter?tags[]=health">health</a>'
        "<p>The department publishes notifications, citizen services and contact "
        "details for district offices across the state.</p></body></html>"
    )

    page = parser().parse("https://karnataka.gov.in/", html)

    assert page.links == ["https://karnataka.gov.in/filter?tags[]=health"]


async def test_a_page_with_a_broken_ipv6_href_still_indexes(store, client):
    body = (
        "<html><head><title>District portal</title></head><body>"
        '<a href="http://[::1/x">broken</a>'
        '<a href="/notices">notices</a>'
        "<p>The department publishes notifications, citizen services and contact "
        "details for district offices across the state.</p></body></html>"
    )

    def handler(request):
        return httpx.Response(200, text=body)

    settings = Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
        robots_enabled=False,
    )
    orchestrator = Orchestrator(client, settings, transport=httpx.MockTransport(handler))
    try:
        await orchestrator.frontier.enqueue(["https://karnataka.gov.in/"])
        outcome = await orchestrator.run_batch()

        assert outcome.indexed_pages == 1
        assert outcome.discovered >= 1
    finally:
        await orchestrator.close()


# ---------------------------------------------------------------------------
# NUL characters and non-HTML bodies
# ---------------------------------------------------------------------------


def test_nul_characters_never_reach_the_database():
    """SQLite truncates TEXT at a NUL: length('a\\x00b') is 1, not 3."""
    collapse = load_agent("03_parser_agent").collapse

    assert collapse("Before\x00after\x00and more\x07text") == "Before after and more text"
    assert "\x00" not in collapse("a\x00b")


async def test_a_page_with_a_nul_still_stores_its_full_text(client):
    """The stored row must not be silently cut at the NUL."""
    from app.engine.store import IndexStore, PageDocument

    collapse = load_agent("03_parser_agent").collapse
    store = IndexStore(client, Settings(environment="test"))
    body = collapse("first half\x00second half of the page text")

    await store.persist_batch(
        [
            PageDocument(
                url="https://a.gov.in/nul",
                domain="a.gov.in",
                title="NUL page",
                description="desc",
                body_text=body,
                digest="d-nul",
            )
        ]
    )

    row = await client.execute("SELECT body_text, length(body_text) FROM pages")
    stored, length = str(row.rows[0][0]), int(row.rows[0][1])

    assert "second half" in stored
    assert length == len(stored), "the database must not truncate the text"


@pytest.mark.parametrize(
    "content_type,expected",
    [
        ("text/html; charset=utf-8", True),
        ("application/xhtml+xml", True),
        ("text/plain", True),
        ("", True),
        ("application/pdf", False),
        ("image/png", False),
        ("application/json", False),
        ("application/zip", False),
        ("video/mp4", False),
    ],
)
def test_only_html_like_bodies_are_parsed(content_type, expected):
    assert fetcher_module.looks_like_html(content_type) is expected


async def test_a_pdf_body_is_rejected_before_parsing():
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7 binary"
        )

    agent = FetcherAgent(
        Settings(environment="test"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        result = await agent.fetch("https://a.gov.in/report")
    finally:
        await agent.aclose()

    assert result.ok is False
    assert result.error.startswith("not_html:")
