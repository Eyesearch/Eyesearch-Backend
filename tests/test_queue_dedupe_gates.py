"""Three gates that stop the queue filling with pages the index already has."""

from __future__ import annotations

import pytest

from app.engine.store import PageDocument
from app.indexer import maintenance

BODY = "a full paragraph of genuine content about the department and its services. " * 6


def page(url: str, digest: str) -> PageDocument:
    return PageDocument(
        url=url, domain=url.split("/")[2], title="T", description="d",
        body_text=BODY, digest=digest, brand_name="A",
    )


async def queue_rows(client):
    result = await client.execute("SELECT url, status, priority FROM crawl_queue ORDER BY url")
    return [(row[0], row[1], int(row[2] or 0)) for row in result.rows]


# ---------------------------------------------------------------------------
# Gate 1 — enqueue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_indexed_url_is_not_queued_again(client, store):
    """ON CONFLICT only deduped against the queue; a page already in `pages`
    was re-queued every time another page linked to it."""
    await store.persist_batch([page("https://a.gov.in/known", "g1-1")])

    added = await store.enqueue_urls([("https://a.gov.in/known", "a.gov.in")])

    assert added == 0
    assert await queue_rows(client) == []


@pytest.mark.asyncio
async def test_a_fresh_url_is_still_queued(client, store):
    await store.persist_batch([page("https://a.gov.in/known", "g1-2")])

    added = await store.enqueue_urls([("https://a.gov.in/new", "a.gov.in")])

    assert added == 1


@pytest.mark.asyncio
async def test_a_mixed_batch_queues_only_the_new_ones(client, store):
    await store.persist_batch([page("https://a.gov.in/known", "g1-3")])

    added = await store.enqueue_urls(
        [
            ("https://a.gov.in/known", "a.gov.in"),
            ("https://a.gov.in/fresh", "a.gov.in"),
            ("https://b.gov.in/fresh", "b.gov.in"),
        ]
    )

    assert added == 2
    assert {row[0] for row in await queue_rows(client)} == {
        "https://a.gov.in/fresh",
        "https://b.gov.in/fresh",
    }


# ---------------------------------------------------------------------------
# Gate 2 — the worker does not spend a slot on an indexed page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_worker_does_not_pop_an_indexed_url(client, store):
    """A row that is already indexed costs a fetch and writes nothing."""
    await store.persist_batch([page("https://a.gov.in/known", "g2-1")])
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, priority) "
        "VALUES ('https://a.gov.in/known', 'a.gov.in', 'pending', 1)"
    )

    assert await store.pending_batch(limit=10) == []


@pytest.mark.asyncio
async def test_a_deliberate_refetch_is_still_popped(client, store):
    """Priority 0 means we chose to fetch this again — a repair, or a re-arm."""
    await store.persist_batch([page("https://a.gov.in/known", "g2-2")])
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, priority) "
        "VALUES ('https://a.gov.in/known', 'a.gov.in', 'pending', 0)"
    )

    popped = await store.pending_batch(limit=10)

    assert len(popped) == 1
    assert popped[0].url == "https://a.gov.in/known"


@pytest.mark.asyncio
async def test_a_batch_is_not_consumed_by_duplicates(client, store):
    """The reported symptom: 60 slots, all spent on already-indexed pages."""
    for index in range(5):
        await store.persist_batch([page(f"https://a.gov.in/old{index}", f"g2-3-{index}")])
        await client.execute(
            "INSERT INTO crawl_queue (url, domain, status, priority) VALUES (?, 'a.gov.in', 'pending', 1)",
            (f"https://a.gov.in/old{index}",),
        )
    await store.enqueue_urls([("https://a.gov.in/fresh", "a.gov.in")])

    popped = await store.pending_batch(limit=10)

    assert [item.url for item in popped] == ["https://a.gov.in/fresh"]


@pytest.mark.asyncio
async def test_re_arming_marks_the_row_as_a_deliberate_refetch(client, store):
    """Otherwise gate two would skip a re-armed row for ever."""
    await store.persist_batch([page("https://a.gov.in/old", "g2-4")])
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, processed_at, priority) "
        "VALUES ('https://a.gov.in/old', 'a.gov.in', 'processed', now() - interval '10 hours', 1)"
    )

    await store.rearm_processed(limit=5, cooldown_seconds=3600)

    rows = await queue_rows(client)
    assert rows[0][1] == "pending"
    assert rows[0][2] == 0
    assert await store.pending_batch(limit=5), "a re-armed row must be poppable"


# ---------------------------------------------------------------------------
# Gate 3 — the sweeper clears a backlog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sweeper_retires_indexed_duplicates(client, store):
    await store.persist_batch([page("https://a.gov.in/known", "g3-1")])
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, priority) "
        "VALUES ('https://a.gov.in/known', 'a.gov.in', 'pending', 1)"
    )

    retired = await maintenance.purge_indexed_duplicates(client)

    assert retired == 1
    assert (await queue_rows(client))[0][1] == "skipped"


@pytest.mark.asyncio
async def test_the_sweeper_leaves_a_deliberate_refetch_alone(client, store):
    await store.persist_batch([page("https://a.gov.in/known", "g3-2")])
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, priority) "
        "VALUES ('https://a.gov.in/known', 'a.gov.in', 'pending', 0)"
    )

    assert await maintenance.purge_indexed_duplicates(client) == 0
    assert (await queue_rows(client))[0][1] == "pending"


@pytest.mark.asyncio
async def test_the_sweeper_is_bounded(client, store):
    """A backlog of days must not lock the queue for minutes in one statement."""
    for index in range(5):
        await store.persist_batch([page(f"https://a.gov.in/p{index}", f"g3-3-{index}")])
        await client.execute(
            "INSERT INTO crawl_queue (url, domain, status, priority) VALUES (?, 'a.gov.in', 'pending', 1)",
            (f"https://a.gov.in/p{index}",),
        )

    first = await maintenance.purge_indexed_duplicates(client, chunk=2)
    second = await maintenance.purge_indexed_duplicates(client, chunk=2)

    assert first == 2
    assert second == 2


@pytest.mark.asyncio
async def test_a_dry_run_retires_nothing(client, store):
    await store.persist_batch([page("https://a.gov.in/known", "g3-4")])
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, priority) "
        "VALUES ('https://a.gov.in/known', 'a.gov.in', 'pending', 1)"
    )

    assert await maintenance.purge_indexed_duplicates(client, dry_run=True) == 1
    assert (await queue_rows(client))[0][1] == "pending"


@pytest.mark.asyncio
async def test_the_sweep_reports_both_halves(client, store):
    result = await maintenance.sweep(client, dry_run=True)

    assert set(result) == {"requeued", "deduplicated"}


@pytest.mark.asyncio
async def test_dead_queue_rows_are_pruned_and_pages_are_not(client, store):
    """A parked or failed row is not work, and it never becomes work.

    The live queue held 89 MB for ~30,000 rows: 13,331 parked at a host's page
    ceiling and 12,000+ dead hosts. Neither will ever be fetched, and both keep
    their slot for ever unless something deletes them. Pages are untouched -
    this deletes from the queue.
    """
    await store.persist_batch([page("https://a.gov.in/kept", "prune-1")])
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, last_error, processed_at, priority) "
        "VALUES ('https://a.gov.in/x1', 'a.gov.in', 'skipped', 'domain_cap', "
        "        now() - interval '60 days', 1)"
    )
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, last_error, processed_at, priority) "
        "VALUES ('https://b.gov.in/x2', 'b.gov.in', 'failed', 'timeout', "
        "        now() - interval '60 days', 1)"
    )
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, priority) "
        "VALUES ('https://c.gov.in/x3', 'c.gov.in', 'pending', 1)"
    )

    dry = await maintenance.prune_dead_queue_rows(client, dry_run=True)
    assert dry == 2
    assert len(await queue_rows(client)) == 3, "a dry run must delete nothing"

    removed = await maintenance.prune_dead_queue_rows(client)
    assert removed == 2

    rows = await queue_rows(client)
    assert len(rows) == 1
    assert rows[0][0] == "https://c.gov.in/x3", "the pending row stays"

    pages = await client.execute("SELECT COUNT(*) FROM pages")
    assert int(pages.scalar(0) or 0) == 1, "no page may be lost"
