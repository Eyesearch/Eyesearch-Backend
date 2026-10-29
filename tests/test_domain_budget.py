"""The shared per-domain body budget.

One domain may hold `domain_body_budget_chars` of body text across all of its
pages, divided evenly between them. Adding a page shrinks everyone's allowance,
including pages that were already stored.
"""

from __future__ import annotations

import pytest

from app.engine.config import Settings
from app.engine.store import IndexStore, PageDocument

BODY = "The department publishes district health notifications for every office. " * 20


def settings(budget: int) -> Settings:
    return Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
        domain_body_budget_chars=budget,
    )


def page(url: str, domain: str, digest: str) -> PageDocument:
    return PageDocument(
        url=url,
        domain=domain,
        title="District health notices",
        description="d",
        body_text=BODY,
        digest=digest,
    )


async def body_lengths(client) -> list[int]:
    result = await client.execute("SELECT length(body_text) FROM pages ORDER BY page_id")
    return [int(row[0]) for row in result.rows]


@pytest.mark.asyncio
async def test_a_lone_page_gets_the_whole_budget(client):
    store = IndexStore(client, settings(1000))
    await store.persist_batch([page("https://a.gov.in/1", "a.gov.in", "b1")])

    assert len(BODY) > 1000, "the fixture must be longer than the budget"
    assert await body_lengths(client) == [1000]


@pytest.mark.asyncio
async def test_the_budget_is_divided_between_the_pages(client):
    store = IndexStore(client, settings(1000))
    await store.persist_batch(
        [page(f"https://a.gov.in/{i}", "a.gov.in", f"b{i}") for i in range(4)]
    )

    lengths = await body_lengths(client)
    assert lengths == [250, 250, 250, 250]
    assert sum(lengths) <= 1000


@pytest.mark.asyncio
async def test_adding_a_page_trims_the_ones_already_stored(client):
    """The budget is per domain, so a new page costs every existing page."""
    store = IndexStore(client, settings(1000))
    await store.persist_batch([page("https://a.gov.in/1", "a.gov.in", "b1")])
    assert await body_lengths(client) == [1000]

    await store.persist_batch([page("https://a.gov.in/2", "a.gov.in", "b2")])
    assert await body_lengths(client) == [500, 500]


@pytest.mark.asyncio
async def test_a_second_domain_has_its_own_budget(client):
    store = IndexStore(client, settings(1000))
    await store.persist_batch(
        [
            page("https://a.gov.in/1", "a.gov.in", "b1"),
            page("https://b.example/1", "b.example", "b2"),
        ]
    )

    # One page each, so each domain spends its own 1,000.
    assert await body_lengths(client) == [1000, 1000]


@pytest.mark.asyncio
async def test_the_allowance_never_reaches_zero(client):
    """40 characters at the 25-page cap, never a negative or zero slice."""
    store = IndexStore(client, settings(1000))
    await store.persist_batch(
        [page(f"https://a.gov.in/{i}", "a.gov.in", f"b{i}") for i in range(25)]
    )

    lengths = await body_lengths(client)
    assert len(lengths) == 25
    assert all(length == 40 for length in lengths)
    assert sum(lengths) <= 1000


@pytest.mark.asyncio
async def test_a_budget_of_zero_disables_the_cap(client):
    store = IndexStore(client, settings(0))
    await store.persist_batch([page("https://a.gov.in/1", "a.gov.in", "b1")])

    assert await body_lengths(client) == [len(BODY)]
