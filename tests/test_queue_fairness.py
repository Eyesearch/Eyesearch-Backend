"""Queue fairness: round-robin dequeue and frontier diversity.

A queue ordered purely by rowid hands every slot in a batch to whichever domain
was queued first. One portal with forty pending URLs takes all ten, and every
other domain waits behind it — the crawl looks busy while it explores one site.

These tests pin the dequeue interleaving, the domain-scoped re-arm, and the
frontier stage that pulls authoritative domains back in when the queue has
drifted elsewhere.
"""

from __future__ import annotations

from app.agents import load_agent
from app.database import initialize_database, verify_writable
from app.engine.config import Settings
from app.libsql_http import LocalSqliteClient

FrontierAgent = load_agent("01_frontier_agent").FrontierAgent


async def _queue(client, rows: list[tuple[str, str]], status: str = "pending") -> None:
    await client.batch(
        [
            (
                "INSERT INTO crawl_queue (url, domain, status) VALUES (?, ?, ?)",
                (url, domain, status),
            )
            for url, domain in rows
        ]
    )


# ---------------------------------------------------------------------------
# Round-robin dequeue
# ---------------------------------------------------------------------------


async def test_the_batch_is_interleaved_across_domains(store, client):
    await _queue(client, [(f"https://busy.gov.in/p{i}", "busy.gov.in") for i in range(40)])
    await _queue(
        client,
        [("https://a.gov.in/x", "a.gov.in"), ("https://a.gov.in/y", "a.gov.in")],
    )
    await _queue(
        client,
        [("https://b.gov.in/x", "b.gov.in"), ("https://b.gov.in/y", "b.gov.in")],
    )

    batch = await store.pending_batch(10)

    domains = [item.domain for item in batch]
    assert len(set(domains[:3])) == 3, f"first three should be three domains, got {domains}"
    assert "a.gov.in" in domains
    assert "b.gov.in" in domains


async def test_the_batch_still_fills_to_its_limit(store, client):
    """A per-domain cap on the row number would shrink the batch."""
    await _queue(client, [(f"https://busy.gov.in/p{i}", "busy.gov.in") for i in range(40)])
    await _queue(client, [("https://a.gov.in/x", "a.gov.in")])
    await _queue(client, [("https://b.gov.in/x", "b.gov.in")])

    batch = await store.pending_batch(10)

    assert len(batch) == 10


async def test_a_single_domain_batch_is_unchanged(store, client):
    await _queue(client, [(f"https://one.gov.in/p{i}", "one.gov.in") for i in range(5)])

    batch = await store.pending_batch(10)

    assert len(batch) == 5
    assert [item.url for item in batch] == [f"https://one.gov.in/p{i}" for i in range(5)]


async def test_only_pending_rows_are_returned(store, client):
    await _queue(client, [("https://a.gov.in/1", "a.gov.in")], status="pending")
    await _queue(client, [("https://a.gov.in/2", "a.gov.in")], status="processed")
    await _queue(client, [("https://a.gov.in/3", "a.gov.in")], status="failed")

    batch = await store.pending_batch(10)

    assert [item.url for item in batch] == ["https://a.gov.in/1"]


async def test_the_batch_is_stable_across_calls(store, client):
    await _queue(client, [(f"https://a.gov.in/p{i}", "a.gov.in") for i in range(3)])

    first = await store.pending_batch(2)
    second = await store.pending_batch(2)

    assert [item.url for item in first] == [item.url for item in second]


# ---------------------------------------------------------------------------
# Domain-scoped re-arm and frontier diversity
# ---------------------------------------------------------------------------


async def test_rearm_can_be_limited_to_named_domains(store, client):
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, processed_at) VALUES "
        "('https://www.thehindu.com/a', 'thehindu.com', 'processed', datetime('now','-7 hours'))"
    )
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, processed_at) VALUES "
        "('https://randomshop.com/a', 'randomshop.com', 'processed', datetime('now','-7 hours'))"
    )

    rearmed = await store.rearm_processed(limit=10, cooldown_seconds=0, domains=["thehindu.com"])

    assert rearmed == 1
    pending = await store.pending_domains()
    assert pending == ["thehindu.com"]


async def test_pending_domains_lists_what_is_waiting(store, client):
    await _queue(client, [("https://a.gov.in/1", "a.gov.in"), ("https://b.gov.in/1", "b.gov.in")])

    domains = await store.pending_domains()

    assert sorted(domains) == ["a.gov.in", "b.gov.in"]


async def test_refill_pulls_authoritative_domains_back_in(store, client, settings):
    """The queue is busy, but only with domains nobody asked for."""
    await _queue(client, [("https://randomshop.com/a", "randomshop.com")])
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, processed_at) VALUES "
        "('https://www.thehindu.com/news/', 'thehindu.com', 'processed', datetime('now','-7 hours'))"
    )
    frontier = FrontierAgent(store, settings)

    added = await frontier.refill(minimum=1)

    assert added == 1
    assert "thehindu.com" in await store.pending_domains()


async def test_refill_leaves_a_healthy_queue_alone(store, client, settings):
    await _queue(client, [("https://karnataka.gov.in/a", "karnataka.gov.in")])
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, processed_at) VALUES "
        "('https://www.thehindu.com/news/', 'thehindu.com', 'processed', datetime('now','-7 hours'))"
    )
    frontier = FrontierAgent(store, settings)

    added = await frontier.refill(minimum=1)

    assert added == 0


async def test_an_authoritative_domain_counts_even_without_a_public_suffix(store, client, settings):
    await _queue(client, [("https://indiankanoon.org/doc/1", "indiankanoon.org")])
    frontier = FrontierAgent(store, settings)

    assert await frontier.has_authoritative_pending() is True


async def test_a_non_authoritative_queue_reports_no_authoritative_work(store, client, settings):
    await _queue(client, [("https://randomshop.com/a", "randomshop.com")])
    frontier = FrontierAgent(store, settings)

    assert await frontier.has_authoritative_pending() is False


# ---------------------------------------------------------------------------
# The write probe
# ---------------------------------------------------------------------------


async def test_the_write_probe_passes_and_leaves_nothing_behind():
    """Five placeholders, five arguments — this was reported as a mismatch."""
    client = LocalSqliteClient(":memory:")
    try:
        await initialize_database(client)

        assert await verify_writable(client) is True

        rows = await client.execute("SELECT COUNT(*) FROM pages")
        assert int(rows.scalar(0)) == 0
    finally:
        await client.close()


async def test_the_write_probe_is_repeatable():
    client = LocalSqliteClient(":memory:")
    try:
        await initialize_database(client)

        assert await verify_writable(client) is True
        assert await verify_writable(client) is True

        rows = await client.execute("SELECT COUNT(*) FROM pages")
        assert int(rows.scalar(0)) == 0
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Capped domains must not crowd the batch
# ---------------------------------------------------------------------------


async def _seed_capped_domains(store, count: int, cap: int) -> None:
    from app.engine.store import PageDocument

    documents = []
    for index in range(count):
        for page in range(cap):
            documents.append(
                PageDocument(
                    url=f"https://site{index}.gov.in/p{page}",
                    domain=f"site{index}.gov.in",
                    title=f"t{index}-{page}",
                    description="d",
                    body_text="b" * 90,
                    digest=f"dg{index}-{page}",
                )
            )
    await store.persist_batch(documents)


async def test_a_capped_domain_does_not_fill_the_batch(store, client):
    """Round-robin alone let capped domains take every slot."""
    await _seed_capped_domains(store, 40, 25)
    await _queue(client, [(f"https://site{i}.gov.in/extra", f"site{i}.gov.in") for i in range(40)])
    await _queue(client, [("https://fresh.gov.in/new", "fresh.gov.in")])

    batch = await store.pending_batch(40)
    domains = [item.domain for item in batch]

    assert "fresh.gov.in" in domains
    assert not any(domain.startswith("site") for domain in domains)


async def test_the_batch_is_empty_when_every_domain_is_capped(store, client):
    await _seed_capped_domains(store, 3, 25)
    await _queue(client, [(f"https://site{i}.gov.in/extra", f"site{i}.gov.in") for i in range(3)])

    assert await store.pending_batch(10) == []


async def test_parking_capped_rows_keeps_the_depth_honest(store, client):
    """Pending rows for a capped domain would otherwise look like real work."""
    await _seed_capped_domains(store, 2, 25)
    await _queue(client, [(f"https://site{i}.gov.in/extra", f"site{i}.gov.in") for i in range(2)])
    await _queue(client, [("https://fresh.gov.in/new", "fresh.gov.in")])

    assert await store.queue_depth() == 3

    parked = await store.skip_pending_for_capped_domains()

    assert parked == 2
    assert await store.queue_depth() == 1
    assert await store.pending_domains() == ["fresh.gov.in"]


async def test_parking_a_single_domain_leaves_others_alone(store, client):
    await _seed_capped_domains(store, 1, 25)
    await _queue(client, [("https://site0.gov.in/extra", "site0.gov.in")])
    await _queue(client, [("https://fresh.gov.in/new", "fresh.gov.in")])

    parked = await store.skip_pending_for_domain("site0.gov.in")

    assert parked == 1
    assert await store.pending_domains() == ["fresh.gov.in"]


# ---------------------------------------------------------------------------
# Bounded enqueue
# ---------------------------------------------------------------------------


class _CountingClient:
    """Wraps a client and records how large each pipeline request was."""

    def __init__(self, inner):
        self.inner = inner
        self.sizes: list[int] = []

    async def execute(self, sql, args=()):
        return await self.inner.execute(sql, args)

    async def batch(self, statements):
        self.sizes.append(len(statements))
        return await self.inner.batch(statements)

    async def close(self):
        await self.inner.close()


async def test_discovered_urls_are_enqueued_in_bounded_chunks(client):
    """One request carrying hundreds of INSERTs overruns Turso's window."""
    from app.engine.store import IndexStore

    counting = _CountingClient(client)
    store = IndexStore(counting, Settings(environment="test", crawl_enqueue_chunk_size=50))

    pairs = [(f"https://site{i}.gov.in/p", f"site{i}.gov.in") for i in range(120)]
    added = await store.enqueue_urls(pairs)

    assert added == 120
    assert counting.sizes == [50, 50, 20]


async def test_chunking_does_not_change_the_added_count(client):
    from app.engine.store import IndexStore

    store = IndexStore(client, Settings(environment="test", crawl_enqueue_chunk_size=7))
    pairs = [(f"https://site{i}.gov.in/p", f"site{i}.gov.in") for i in range(20)]

    first = await store.enqueue_urls(pairs)
    second = await store.enqueue_urls(pairs)

    assert first == 20
    assert second == 0  # INSERT OR IGNORE: the second pass adds nothing


async def test_a_single_chunk_when_everything_fits(client):
    from app.engine.store import IndexStore

    counting = _CountingClient(client)
    store = IndexStore(counting, Settings(environment="test", crawl_enqueue_chunk_size=50))

    await store.enqueue_urls([("https://a.gov.in/1", "a.gov.in")])

    assert counting.sizes == [1]
