"""Atomic persistence, the confirmed-commit counter, and the storage guard.

The rules under test:

* a page write and its `crawl_queue` transition land in one batch, or neither
  lands;
* a page is only counted once the database has confirmed the commit;
* the storage guard reads counts at most once per TTL and fails closed.
"""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.engine.store import IndexStore, PageDocument
from app.indexer.guard import StorageGuard
from app.libsql_http import LibsqlError
from tests.conftest import make_document

indexer_module = load_agent("05_indexer_agent")
IndexerAgent = indexer_module.IndexerAgent


async def _queue(client, url: str, domain: str = "karnataka.gov.in") -> int:
    result = await client.execute(
        "INSERT INTO crawl_queue (url, domain, status) VALUES (?, ?, 'pending')",
        (url, domain),
    )
    queued = await client.execute("SELECT rowid FROM crawl_queue WHERE url = ?", (url,))
    return int(queued.scalar(0) or result.rows_affected)


async def _queue_status(client, url: str) -> str:
    result = await client.execute("SELECT status FROM crawl_queue WHERE url = ?", (url,))
    return str(result.scalar("missing"))


async def test_a_page_write_and_its_queue_transition_land_together(store, client):
    url = "https://karnataka.gov.in/"
    rowid = await _queue(client, url)
    document = make_document(url=url, digest="digest-one")

    outcome = await store.persist_batch([document], [rowid])

    assert outcome.committed is True
    assert outcome.pages_written == 1
    assert outcome.queue_closed == 1
    assert outcome.total_pages == 1
    assert await _queue_status(client, url) == "processed"


async def test_site_payload_budget_counts_utf8_bytes_for_all_stored_fields(store, client):
    url = "https://karnataka.gov.in/"
    rowid = await _queue(client, url)
    document = make_document(
        url=url,
        title="ಕರ್ನಾಟಕ",
        description="Government portal",
        body_text="ಬೆಂಗಳೂರು ಮಾಹಿತಿ",
        digest="digest-payload",
    )

    await store.persist_batch([document], [rowid])

    usage = await store.domain_payload_bytes(["karnataka.gov.in"])
    assert usage["karnataka.gov.in"] == store.page_payload_bytes(document)
    assert store.page_payload_bytes(document) > len(document.title) + len(document.body_text)


async def test_the_reported_page_count_is_read_back_from_the_database(store, client):
    rowids = [await _queue(client, f"https://a{index}.gov.in/") for index in range(3)]
    documents = [
        make_document(url=f"https://a{index}.gov.in/", domain=f"a{index}.gov.in", digest=f"d{index}")
        for index in range(3)
    ]

    outcome = await store.persist_batch(documents, rowids)

    counted = await client.execute("SELECT COUNT(*) FROM pages")
    assert outcome.total_pages == int(counted.scalar(0))
    assert outcome.total_pages == 3


async def test_the_index_is_searchable_immediately_after_a_commit(store, client):
    """Proves the FTS triggers exist: without them the write is invisible."""
    url = "https://karnataka.gov.in/"
    rowid = await _queue(client, url)

    await store.persist_batch([make_document(url=url, digest="digest-fts")], [rowid])

    hits = await store.search("karnataka")
    assert [hit.url for hit in hits] == [url]


async def test_a_repeated_url_is_skipped_and_reported_as_a_duplicate(store, client):
    url = "https://karnataka.gov.in/"
    rowid = await _queue(client, url)

    first = await store.persist_batch([make_document(url=url, digest="digest-first")], [rowid])

    # The frontier re-arms a finished domain, so the same URL is offered again.
    await client.execute("UPDATE crawl_queue SET status = 'pending' WHERE rowid = ?", (rowid,))
    second = await store.persist_batch(
        [make_document(url=url, digest="digest-second")], [rowid]
    )

    assert first.pages_written == 1
    assert second.committed is True
    assert second.pages_written == 0
    assert second.duplicates == 1


async def test_identical_body_text_under_another_url_is_rejected(store, client):
    body = (
        "Karnataka is a state in south western India, and Bengaluru is its capital "
        "city. The portal publishes notifications and citizen services."
    )
    first_url = "https://a.gov.in/"
    second_url = "https://b.gov.in/"
    first_rowid = await _queue(client, first_url, "a.gov.in")
    second_rowid = await _queue(client, second_url, "b.gov.in")

    await store.persist_batch(
        [make_document(url=first_url, domain="a.gov.in", body_text=body, digest="same")],
        [first_rowid],
    )
    second = await store.persist_batch(
        [make_document(url=second_url, domain="b.gov.in", body_text=body, digest="same")],
        [second_rowid],
    )

    assert second.pages_written == 0
    total = await client.execute("SELECT COUNT(*) FROM pages")
    assert int(total.scalar(0)) == 1


class _ExplodingClient:
    """Fails the batch at the page insert, after the transaction opened."""

    def __init__(self, inner, marker: str = "INSERT OR IGNORE INTO pages") -> None:
        self.inner = inner
        self.marker = marker

    async def execute(self, sql, args=()):
        return await self.inner.execute(sql, args)

    async def batch(self, statements):
        for sql, _args in statements:
            if self.marker in sql:
                raise LibsqlError(f"boom (statement: {sql})")
        return await self.inner.batch(statements)

    async def close(self):
        await self.inner.close()


async def test_a_failed_statement_leaves_no_page_and_no_queue_transition(client):
    url = "https://karnataka.gov.in/"
    rowid = await _queue(client, url)
    store = IndexStore(_ExplodingClient(client))

    outcome = await store.persist_batch([make_document(url=url, digest="d")], [rowid])

    assert outcome.committed is False
    assert outcome.pages_written == 0

    pages = await client.execute("SELECT COUNT(*) FROM pages")
    assert int(pages.scalar(0)) == 0
    assert await _queue_status(client, url) == "pending"


async def test_a_failed_queue_row_is_parked_rather_than_left_pending(store, client):
    url = "https://blocked.gov.in/"
    rowid = await _queue(client, url, "blocked.gov.in")

    failed = await store.mark_failed([rowid], reason="blocked_status:403")

    assert failed == 1
    assert await _queue_status(client, url) == "failed"
    retries = await client.execute("SELECT retry_count FROM crawl_queue WHERE rowid = ?", (rowid,))
    assert int(retries.scalar(0)) == 1


async def test_the_indexer_reports_zero_counted_pages_when_the_write_failed(client):
    """The counter may only move on a confirmed commit."""
    url = "https://karnataka.gov.in/"
    rowid = await _queue(client, url)
    store = IndexStore(_ExplodingClient(client))
    guard = StorageGuard(client, limit=300_000, ttl_seconds=60.0)
    indexer = IndexerAgent(store, guard)

    outcome = await indexer.persist([make_document(url=url, digest="d")], [rowid])

    assert outcome.committed is False
    assert outcome.counted == 0


async def test_the_indexer_counts_pages_only_after_a_confirmed_commit(client):
    url = "https://karnataka.gov.in/"
    rowid = await _queue(client, url)
    store = IndexStore(client)
    guard = StorageGuard(client, limit=300_000, ttl_seconds=60.0)
    indexer = IndexerAgent(store, guard)

    outcome = await indexer.persist([make_document(url=url, digest="d")], [rowid])

    assert outcome.committed is True
    assert outcome.counted == 1


async def test_the_guard_reads_the_database_once_per_ttl(client):
    guard = StorageGuard(client, limit=300_000, ttl_seconds=60.0)

    first = await guard.storage_state()
    second = await guard.storage_state()
    third = await guard.storage_state()

    assert guard.count_queries == 1
    assert first.pages == 0
    assert second.from_cache is True
    assert third.from_cache is True


async def test_the_guard_re_reads_after_the_ttl_expires(client):
    now = [1000.0]
    guard = StorageGuard(client, limit=300_000, ttl_seconds=60.0, clock=lambda: now[0])

    await guard.storage_state()
    now[0] += 61.0
    await guard.storage_state()

    assert guard.count_queries == 2


async def test_the_guard_pauses_at_the_ceiling(client):
    guard = StorageGuard(client, limit=0, ttl_seconds=60.0)

    state = await guard.storage_state()

    assert state.paused is True
    assert state.over_limit is True
    assert await guard.can_write() is False


async def test_the_guard_fails_closed_when_the_count_cannot_be_read(client):
    class _Broken:
        async def execute(self, sql, args=()):
            raise LibsqlError("connection refused")

        async def batch(self, statements):
            raise LibsqlError("connection refused")

        async def close(self):
            pass

    guard = StorageGuard(_Broken(), limit=300_000, ttl_seconds=60.0)

    state = await guard.storage_state()

    assert state.paused is True
    assert "unavailable" in state.reason
    assert await guard.can_write() is False


async def test_a_paused_guard_blocks_the_write_entirely(client):
    url = "https://karnataka.gov.in/"
    rowid = await _queue(client, url)
    store = IndexStore(client)
    indexer = IndexerAgent(store, StorageGuard(client, limit=0, ttl_seconds=60.0))

    outcome = await indexer.persist([make_document(url=url, digest="d")], [rowid])

    assert outcome.committed is False
    assert outcome.paused is True
    assert await _queue_status(client, url) == "pending"


async def test_the_pending_batch_is_ten_urls_ordered_by_rowid(store, client):
    for index in range(12):
        await _queue(client, f"https://site{index}.gov.in/", f"site{index}.gov.in")

    batch = await store.pending_batch(10)

    assert len(batch) == 10
    assert [item.rowid for item in batch] == sorted(item.rowid for item in batch)


async def test_a_document_row_keeps_the_body_inside_the_budget(store, client):
    url = "https://long.gov.in/"
    rowid = await _queue(client, url, "long.gov.in")
    body = " ".join(["karnataka"] * 400)[:1000]
    document: PageDocument = make_document(url=url, domain="long.gov.in", body_text=body, digest="d")

    await store.persist_batch([document], [rowid])

    stored = await store.get_page(url)
    assert stored is not None
    assert len(stored["body_text"]) <= 1000


async def test_the_store_raises_nothing_when_there_is_nothing_to_write(store):
    outcome = await store.persist_batch([], [])

    assert outcome.committed is True
    assert outcome.pages_written == 0


@pytest.mark.parametrize("queue_ids", [[], [999]])
async def test_an_unknown_queue_row_does_not_fail_the_page_write(store, client, queue_ids):
    url = "https://karnataka.gov.in/"
    await _queue(client, url)

    outcome = await store.persist_batch([make_document(url=url, digest="d")], queue_ids)

    assert outcome.committed is True
    assert outcome.pages_written == 1
