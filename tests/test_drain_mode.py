"""Repair mode: stop discovering, empty the backlog, then resume."""

from __future__ import annotations

import httpx
import pytest

from app.engine.config import Settings
from app.engine.store import PageDocument
from app.indexer import maintenance
from app.orchestrator import Orchestrator

BODY = "genuine content about the department and the services it offers. " * 6


def settings(**overrides) -> Settings:
    base = dict(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
        page_body_char_ceiling=0,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def page(url: str, digest: str) -> PageDocument:
    return PageDocument(
        url=url, domain="a.gov.in", title="T", description="d",
        body_text=BODY, digest=digest, brand_name="A",
    )


LINKED = (
    "<html><head><title>Page</title></head><body>"
    '<a href="/new-one">1</a><a href="/new-two">2</a>'
    "<p>" + BODY + "</p></body></html>"
)


@pytest.mark.asyncio
async def test_drain_mode_queues_nothing_new(client, store):
    """The links are found and deliberately dropped."""
    await store.enqueue_urls([("https://a.gov.in/start", "a.gov.in")])

    orchestrator = Orchestrator(
        client, settings(crawler_drain_mode=True),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=LINKED)),
    )
    try:
        outcome = await orchestrator.run_batch()

        assert outcome.discovered == 0
        assert outcome.drain_dropped >= 1
    finally:
        await orchestrator.close()


@pytest.mark.asyncio
async def test_without_drain_mode_the_links_are_queued(client, store):
    await store.enqueue_urls([("https://a.gov.in/start", "a.gov.in")])

    orchestrator = Orchestrator(
        client, settings(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=LINKED)),
    )
    try:
        outcome = await orchestrator.run_batch()

        assert outcome.discovered >= 1
        assert outcome.drain_dropped == 0
    finally:
        await orchestrator.close()


@pytest.mark.asyncio
async def test_drain_mode_still_fetches_repair_rows(client, store):
    """Stopping discovery must not stop crawling the backlog."""
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, priority) "
        "VALUES ('https://a.gov.in/legacy', 'a.gov.in', 'pending', ?)",
        (maintenance.REPAIR_PRIORITY,),
    )

    orchestrator = Orchestrator(
        client, settings(crawler_drain_mode=True),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=LINKED)),
    )
    try:
        outcome = await orchestrator.run_batch()

        assert outcome.attempted == 1
        assert outcome.queue_closed == 1
    finally:
        await orchestrator.close()


@pytest.mark.asyncio
async def test_drain_mode_does_not_read_sitemaps(client, store):
    """A sitemap is the largest source of new URLs; reading one would refill
    the frontier we are emptying."""
    orchestrator = Orchestrator(client, settings(crawler_drain_mode=True))
    try:
        orchestrator._schedule_sitemap_harvest(["a.gov.in"])

        assert orchestrator._pending_sitemap_domains == set()
        assert orchestrator._sitemap_harvest_task is None
    finally:
        await orchestrator.close()


@pytest.mark.asyncio
async def test_the_backlog_report_counts_repair_rows(client):
    """Hermetic: the rows are cleared first, and the priorities come from the
    constant rather than a literal.

    A test that hardcodes 0 stops testing the function and starts testing the
    fixture's leftovers — and if the constant ever moves, it fails with a count
    that says nothing about which of the two changed.
    """
    await client.execute("DELETE FROM crawl_queue WHERE url LIKE 'https://a.gov.in/%'")

    repair = maintenance.REPAIR_PRIORITY
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, priority) VALUES (?, ?, 'pending', ?), "
        "(?, ?, 'pending', ?), (?, ?, 'pending', ?)",
        (
            "https://a.gov.in/1", "a.gov.in", repair,
            "https://a.gov.in/2", "a.gov.in", repair,
            "https://a.gov.in/3", "a.gov.in", repair + 1,
        ),
    )

    backlog = await maintenance.repair_backlog(client)

    assert backlog["pending_total"] == 3
    assert backlog["pending_repair"] == 2
    assert backlog["estimated_minutes"] >= 0


def test_repair_priority_is_zero():
    """The architectural contract: 0 is reserved for repair, discovery is >= 1."""
    assert maintenance.REPAIR_PRIORITY == 0


def test_the_setting_defaults_to_off():
    assert Settings(_env_file=None).crawler_drain_mode is False
