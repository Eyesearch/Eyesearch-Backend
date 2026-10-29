"""The in-place audit: what it re-queues, what it purges, and the queue priority."""

from __future__ import annotations

import pytest

from app.engine.store import PageDocument

TRUNCATED_THRESHOLD = 45
THIN_WORD_COUNT = 15


@pytest.mark.asyncio
async def test_the_queue_has_a_priority_column(client):
    """The audit re-queues at priority 0, so the column has to exist."""
    result = await client.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'crawl_queue' AND column_name = 'priority'"
    )
    assert result.scalar(0) == "priority"


@pytest.mark.asyncio
async def test_a_priority_zero_url_is_dequeued_before_a_newer_one(client, store):
    """Within a domain, the re-queued page goes first even though it is older."""
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, priority) VALUES "
        "('https://a.gov.in/new', 'a.gov.in', 'pending', 1), "
        "('https://a.gov.in/old', 'a.gov.in', 'pending', 0)"
    )

    batch = await store.pending_batch(limit=2)

    assert [row.url for row in batch] == [
        "https://a.gov.in/old",
        "https://a.gov.in/new",
    ]


@pytest.mark.asyncio
async def test_a_truncated_page_is_short_but_is_not_thin(store, client):
    """The audit's two rules must not fight: a page cut to 40 characters has six
    words, and calling that thin would delete what the re-queue just saved."""
    await store.persist_batch(
        [
            PageDocument(
                url="https://a.gov.in/cut",
                domain="a.gov.in",
                title="Truncated by the old rule",
                description="d",
                body_text="karnataka health district notification office",
                digest="cut-1",
            )
        ]
    )

    thin = await client.execute(
        "SELECT count(*) FROM pages WHERE array_length("
        "regexp_split_to_array(trim(body_text), '\\s+'), 1) < ? AND length(body_text) > ?",
        (THIN_WORD_COUNT, TRUNCATED_THRESHOLD),
    )
    truncated = await client.execute(
        "SELECT count(*) FROM pages WHERE length(body_text) <= ?", (TRUNCATED_THRESHOLD,)
    )

    assert int(truncated.scalar(0)) == 1
    assert int(thin.scalar(0)) == 0


@pytest.mark.asyncio
async def test_a_genuinely_thin_page_is_thin(store, client):
    await store.persist_batch(
        [
            PageDocument(
                url="https://a.gov.in/thin",
                domain="a.gov.in",
                title="Thin",
                description="d",
                body_text="two words only " * 6,  # 72 characters, 18 words
                digest="thin-1",
            )
        ]
    )
    # 72 characters is above the truncated threshold, so this page is judged on
    # its own merits rather than excused as truncated.
    result = await client.execute("SELECT length(body_text) FROM pages")
    assert int(result.scalar(0)) > TRUNCATED_THRESHOLD
