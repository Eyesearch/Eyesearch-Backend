"""Universal spider traps: error pages, auth walls, facets, recursion, redirects."""

from __future__ import annotations

import httpx
import pytest

from app.agents import load_agent
from app.engine.config import Settings

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
