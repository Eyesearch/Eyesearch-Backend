"""The three database management agents and the search cache TTL."""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.engine.config import Settings
from app.engine.store import IndexStore, PageDocument
from app.services.search_service import CACHE_TTL_SECONDS, SearchCache

RuleEnforcerAgent = load_agent("11_rule_enforcer_agent").RuleEnforcerAgent
strip_tracking = load_agent("11_rule_enforcer_agent").strip_tracking
CategoryAuditorAgent = load_agent("12_category_auditor_agent").CategoryAuditorAgent
DedupeCompactAgent = load_agent("13_dedupe_compact_agent").DedupeCompactAgent
canonical_path = load_agent("13_dedupe_compact_agent").canonical_path
ClassifierAgent = load_agent("07_classifier_agent").ClassifierAgent


def settings(cap: int = 25, ceiling: int = 0) -> Settings:
    return Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
        max_pages_per_domain=cap,
        page_body_char_ceiling=ceiling,
    )


def doc(url: str, domain: str, digest: str, quality: float = 1.0, category: str = "general_reference"):
    return PageDocument(
        url=url,
        domain=domain,
        title="A page",
        description="d",
        body_text="body text " * 30,
        digest=digest,
        quality_score=quality,
        category=category,
    )


async def count(client) -> int:
    return int((await client.execute("SELECT count(*) FROM pages")).scalar(0) or 0)


# ---------------------------------------------------------------------------
# Rule enforcer
# ---------------------------------------------------------------------------


def test_tracking_parameters_are_stripped():
    assert strip_tracking("https://a.com/x?utm_source=news&ref=tw") == "https://a.com/x"
    assert strip_tracking("https://a.com/x?sid=abc&page=2") == "https://a.com/x?page=2"
    assert strip_tracking("https://a.com/x") == "https://a.com/x"


@pytest.mark.asyncio
async def test_a_domain_over_the_cap_loses_its_weakest_pages(client):
    store = IndexStore(client, settings(cap=3))
    await store.persist_batch(
        [doc(f"https://a.gov.in/p{i}", "a.gov.in", f"d{i}", quality=1.0 - i * 0.1) for i in range(5)]
    )

    report = await RuleEnforcerAgent(client, settings(cap=3)).run()

    assert report.over_cap_domains == 1
    assert report.pages_purged == 2
    assert await count(client) == 3
    # The best page survives; the two weakest are the ones that went.
    kept = await client.execute("SELECT url FROM pages ORDER BY page_id")
    assert [row[0] for row in kept.rows] == [
        "https://a.gov.in/p0",
        "https://a.gov.in/p1",
        "https://a.gov.in/p2",
    ]


@pytest.mark.asyncio
async def test_a_domain_inside_the_cap_is_left_alone(client):
    store = IndexStore(client, settings(cap=3))
    await store.persist_batch([doc("https://a.gov.in/1", "a.gov.in", "d1")])

    report = await RuleEnforcerAgent(client, settings(cap=3)).run()

    assert report.pages_purged == 0
    assert await count(client) == 1


@pytest.mark.asyncio
async def test_pages_without_a_site_do_not_purge_each_other(client):
    """A row with no site_id has no domain, so it is not ranked against others."""
    await client.execute(
        "INSERT INTO pages (url, title, body_text, digest, site_id) VALUES "
        "('https://x.example/1', 'A', 'b', 'n1', NULL), "
        "('https://y.example/1', 'B', 'b', 'n2', NULL), "
        "('https://z.example/1', 'C', 'b', 'n3', NULL)"
    )

    report = await RuleEnforcerAgent(client, settings(cap=1)).run()

    assert report.pages_purged == 0
    assert await count(client) == 3


@pytest.mark.asyncio
async def test_over_long_bodies_are_trimmed(client):
    store = IndexStore(client, settings(ceiling=0))
    await store.persist_batch([doc("https://a.gov.in/1", "a.gov.in", "d1")])

    report = await RuleEnforcerAgent(client, settings(ceiling=40)).enforce_body_ceiling()

    assert report == 1
    length = await client.execute("SELECT length(body_text) FROM pages")
    assert int(length.scalar(0)) == 40


@pytest.mark.asyncio
async def test_a_tracked_url_is_cleaned_rather_than_duplicated(client):
    store = IndexStore(client, settings(ceiling=0))
    await store.persist_batch([doc("https://a.gov.in/x?utm_source=news", "a.gov.in", "d1")])

    cleaned, removed = await RuleEnforcerAgent(client, settings()).clean_urls()

    assert (cleaned, removed) == (1, 0)
    url = await client.execute("SELECT url FROM pages")
    assert url.scalar(0) == "https://a.gov.in/x"


@pytest.mark.asyncio
async def test_a_tracked_url_whose_target_exists_is_deleted(client):
    store = IndexStore(client, settings(ceiling=0))
    await store.persist_batch(
        [
            doc("https://a.gov.in/x", "a.gov.in", "d1"),
            doc("https://a.gov.in/x?ref=tw", "a.gov.in", "d2"),
        ]
    )

    cleaned, removed = await RuleEnforcerAgent(client, settings()).clean_urls()

    assert (cleaned, removed) == (1, 1)
    assert await count(client) == 1


# ---------------------------------------------------------------------------
# Category auditor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_misrouted_page_is_corrected(client):
    store = IndexStore(client, settings(ceiling=0))
    await store.persist_batch([doc("https://karnataka.gov.in/scheme", "karnataka.gov.in", "d1")])

    report = await CategoryAuditorAgent(client, ClassifierAgent()).run()

    assert report.inspected == 1
    assert report.corrected == 1
    category = await client.execute("SELECT category FROM pages")
    assert category.scalar(0) == "government"


@pytest.mark.asyncio
async def test_a_dry_run_changes_nothing(client):
    store = IndexStore(client, settings(ceiling=0))
    await store.persist_batch([doc("https://karnataka.gov.in/scheme", "karnataka.gov.in", "d1")])

    report = await CategoryAuditorAgent(client, ClassifierAgent()).run(dry_run=True)

    assert report.corrected == 1
    category = await client.execute("SELECT category FROM pages")
    assert category.scalar(0) == "general_reference"


# ---------------------------------------------------------------------------
# Dedupe and compaction
# ---------------------------------------------------------------------------


def test_canonical_path_ignores_query_trailing_slash_and_www():
    assert canonical_path("https://www.a.com/x/?utm=1") == "a.com/x"
    assert canonical_path("https://a.com/x") == "a.com/x"


@pytest.mark.asyncio
async def test_the_same_path_under_two_urls_keeps_the_better_copy(client):
    store = IndexStore(client, settings(ceiling=0))
    await store.persist_batch(
        [
            doc("https://a.com/x", "a.com", "d1", quality=2.0),
            doc("https://a.com/x/", "a.com", "d2", quality=1.0),
        ]
    )

    report = await DedupeCompactAgent(client).run()

    assert report.duplicate_paths == 1
    assert await count(client) == 1
    kept = await client.execute("SELECT url FROM pages")
    assert kept.scalar(0) == "https://a.com/x"


@pytest.mark.asyncio
async def test_the_digest_index_already_prevents_digest_duplicates(client):
    """The unique index makes these impossible; the agent is the safety net for
    a database that predates it."""
    store = IndexStore(client, settings(ceiling=0))
    await store.persist_batch([doc("https://a.com/1", "a.com", "same-digest")])
    # The insert is ON CONFLICT DO NOTHING, so the second page is skipped rather
    # than raising: the index makes the duplicate impossible to create.
    await store.persist_batch([doc("https://b.com/1", "b.com", "same-digest")])

    assert await count(client) == 1
    assert await DedupeCompactAgent(client).by_digest() == 0


# ---------------------------------------------------------------------------
# Search cache
# ---------------------------------------------------------------------------


def test_the_search_cache_holds_for_sixty_seconds():
    assert CACHE_TTL_SECONDS == 60.0


def test_the_cache_serves_a_repeated_query_and_expires():
    now = [0.0]
    cache = SearchCache(ttl_seconds=60.0, clock=lambda: now[0])
    cache.set("k", {"results": [1]})

    assert cache.get("k") == {"results": [1]}
    now[0] = 59.0
    assert cache.get("k") is not None
    now[0] = 61.0
    assert cache.get("k") is None
