"""One malformed URL must cost one URL, never the batch or the crawl loop."""

from __future__ import annotations

import httpx
import pytest

from app.agents import load_agent
from app.engine.config import Settings
from app.orchestrator import LOOP_ERROR_BACKOFF_SECONDS, Orchestrator

fetcher_module = load_agent("02_fetcher_agent")
FetcherAgent = fetcher_module.FetcherAgent
sanitise_url = fetcher_module.sanitise_url

MALFORMED = "http://[::1/ malformed"


# ---------------------------------------------------------------------------
# URL repair
# ---------------------------------------------------------------------------


def test_a_space_in_the_path_is_percent_encoded():
    assert sanitise_url("https://a.gov.in/a b") == "https://a.gov.in/a%20b"


def test_a_missing_path_becomes_a_slash():
    assert sanitise_url("https://a.gov.in") == "https://a.gov.in/"


def test_an_already_encoded_path_is_not_double_encoded():
    assert sanitise_url("https://a.gov.in/a%20b") == "https://a.gov.in/a%20b"


def test_a_query_string_is_preserved():
    assert sanitise_url("https://a.gov.in/x?q=1&r=2") == "https://a.gov.in/x?q=1&r=2"


@pytest.mark.parametrize(
    "url", [MALFORMED, "/relative/path", "", "   ", "not a url at all"]
)
def test_an_unrepairable_url_returns_none(url):
    assert sanitise_url(url) is None


# ---------------------------------------------------------------------------
# fetch / fetch_many
# ---------------------------------------------------------------------------


async def test_fetch_returns_a_result_for_a_malformed_url():
    """httpx.InvalidURL is not an HTTPError, so it used to escape fetch()."""
    agent = FetcherAgent(Settings(environment="test"))
    try:
        result = await agent.fetch(MALFORMED)
    finally:
        await agent.aclose()

    assert result.ok is False
    assert result.error == "invalid_url"


async def test_one_bad_url_does_not_cost_the_whole_batch():
    def handler(request):
        return httpx.Response(200, text="<html><body><p>" + "x" * 200 + "</p></body></html>")

    agent = FetcherAgent(
        Settings(environment="test"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        results = await agent.fetch_many(
            ["https://a.gov.in/1", MALFORMED, "https://b.gov.in/2"]
        )
    finally:
        await agent.aclose()

    assert len(results) == 3
    assert results[0].ok is True
    assert results[1].ok is False
    assert results[1].error == "invalid_url"
    assert results[2].ok is True


async def test_a_gather_level_failure_is_converted_to_a_result(monkeypatch):
    """Belt and braces: if fetch() ever raises again, the batch still survives."""
    agent = FetcherAgent(Settings(environment="test"))

    async def boom(url, *, attempts=1):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(agent, "fetch", boom)
    try:
        results = await agent.fetch_many(["https://a.gov.in/1"])
    finally:
        await agent.aclose()

    assert len(results) == 1
    assert results[0].error == "gather_error:RuntimeError"


# ---------------------------------------------------------------------------
# The crawl loop
# ---------------------------------------------------------------------------


def test_the_loop_does_not_freeze_for_a_minute_on_an_error():
    assert LOOP_ERROR_BACKOFF_SECONDS <= 10.0, (
        "a 60s pause after an unexpected error is indistinguishable from a stall"
    )


async def test_a_malformed_url_in_the_queue_does_not_break_the_batch(store, client):
    body = (
        "<html><head><title>Portal</title></head><body><p>"
        "The department publishes notifications, citizen services and contact "
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
        added = await orchestrator.frontier.enqueue(
            [MALFORMED, "https://karnataka.gov.in/notice"]
        )

        # The malformed URL is dropped at admission, before it can reach the
        # fetcher: it never occupies a queue slot or a page quota.
        assert added == 1
        queued = await client.execute("SELECT url FROM crawl_queue")
        assert [str(row[0]) for row in queued.rows] == ["https://karnataka.gov.in/notice"]

        outcome = await orchestrator.run_batch()

        # And the good URL is indexed normally.
        assert outcome.indexed_pages == 1
        assert outcome.attempted == 1
    finally:
        await orchestrator.close()


def test_discovery_survives_a_malformed_link():
    """A broken href must be dropped, not crash the link filter."""
    from app.agents import load_agent as _load

    frontier = _load("01_frontier_agent")

    assert frontier.domain_of(MALFORMED) == ""
    assert frontier.is_crawlable(MALFORMED) is False
    assert frontier.normalise_url(MALFORMED) is None
    # A good link in the same document still comes through.
    assert frontier.normalise_url("https://karnataka.gov.in/notice") is not None
