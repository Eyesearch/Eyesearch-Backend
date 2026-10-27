"""Frontier link discovery.

The crawler is only recursive if a fetched page feeds its own outbound links
back into `crawl_queue`. Without this the queue drains after the seeds and the
worker idles forever, which is exactly what happened in production.

These tests cover the three stages: the parser extracts absolute links, the
frontier applies crawl scope, and the orchestrator queues what survives.
"""

from __future__ import annotations

import httpx

from app.agents import load_agent
from app.engine.config import Settings
from app.orchestrator import Orchestrator

frontier_module = load_agent("01_frontier_agent")
parser_module = load_agent("03_parser_agent")

FrontierAgent = frontier_module.FrontierAgent
MAX_LINKS_PER_PAGE = parser_module.MAX_LINKS_PER_PAGE
MAX_PENDING_QUEUE = frontier_module.MAX_PENDING_QUEUE
ParserAgent = parser_module.ParserAgent
filter_links = FrontierAgent.filter_links
in_crawl_scope = frontier_module.in_crawl_scope
is_crawlable = frontier_module.is_crawlable
normalise_url = frontier_module.normalise_url

SEED = "https://karnataka.gov.in/"
SEED_URLS = frontier_module.SEED_URLS

PAGE = """
<html><head><title>Karnataka State Portal</title>
<meta name="description" content="Official portal of the Government of Karnataka."></head>
<body>
  <a href="/about">About</a>
  <a href="/departments/health">Health department</a>
  <a href="https://india.gov.in/services">National services</a>
  <a href="/logo.png">Logo</a>
  <a href="/a/b/c/d/e">Too deep</a>
  <a href="/login">Sign in</a>
  <a href="https://facebook.com/karnataka">Facebook</a>
  <a href="#top">Top</a>
  <a href="mailto:info@karnataka.gov.in">Mail</a>
  <a href="/about#history">About, again</a>
  <p>Karnataka is a state in south western India and Bengaluru is its capital city.
     The portal publishes notifications and citizen services for residents.</p>
</body></html>
"""


def test_the_parser_extracts_absolute_links_and_drops_non_navigable_hrefs():
    """Extraction only. Crawl scope — extensions, depth, host — is the
    frontier's job, so a `.png` legitimately survives this stage."""
    parsed = ParserAgent().parse(SEED, PAGE)

    assert SEED + "about" in parsed.links
    assert SEED + "departments/health" in parsed.links
    assert "https://india.gov.in/services" in parsed.links

    assert not any(link.startswith("mailto:") for link in parsed.links)
    assert not any("#" in link for link in parsed.links)

    # ...and the frontier is what removes the rest.
    kept = FrontierAgent.filter_links(None, parsed.links, SEED)
    assert SEED + "logo.png" not in kept
    assert SEED + "login" not in kept
    assert SEED + "a/b/c/d/e/f/g/h" not in kept
    assert "https://facebook.com/karnataka" not in kept


def test_relative_links_are_resolved_against_the_page_url():
    parsed = ParserAgent().parse("https://karnataka.gov.in/departments/", PAGE)

    assert "https://karnataka.gov.in/about" in parsed.links


def test_the_parser_caps_the_links_it_returns():
    html = "<html><body>" + "".join(f'<a href="/p{i}">p</a>' for i in range(200)) + "</body></html>"

    parsed = ParserAgent().parse(SEED, html)

    assert len(parsed.links) == MAX_LINKS_PER_PAGE


def test_normalise_url_collapses_variants_of_the_same_page():
    assert normalise_url("https://www.karnataka.gov.in/about/?utm_source=x#top") == (
        "https://karnataka.gov.in/about"
    )
    assert normalise_url("https://karnataka.gov.in/about") == (
        "https://karnataka.gov.in/about"
    )
    assert normalise_url("mailto:a@b.in") is None


def test_is_crawlable_rejects_junk_extensions_and_deep_paths():
    assert is_crawlable("https://karnataka.gov.in/about") is True
    assert is_crawlable("https://karnataka.gov.in/departments/health") is True

    assert is_crawlable("https://karnataka.gov.in/en/departments/health/services") is True

    assert is_crawlable("https://karnataka.gov.in/logo.png") is False
    assert is_crawlable("https://karnataka.gov.in/report.pdf") is False
    assert is_crawlable("https://karnataka.gov.in/login") is False
    # Deeper than MAX_PATH_DEPTH (6) is still out of scope.
    assert is_crawlable("https://karnataka.gov.in/a/b/c/d/e/f/g/h") is False


def test_crawl_scope_is_the_same_host_or_an_indian_public_domain():
    assert in_crawl_scope("https://karnataka.gov.in/about", "karnataka.gov.in") is True
    assert in_crawl_scope("https://india.gov.in/services", "karnataka.gov.in") is True
    assert in_crawl_scope("https://facebook.com/karnataka", "karnataka.gov.in") is False
    assert in_crawl_scope("https://example.com/x", "karnataka.gov.in") is False


def test_filter_links_keeps_only_what_is_in_scope():
    links = [
        SEED + "about",
        SEED + "about#history",
        SEED + "logo.png",
        SEED + "a/b/c/d/e/f/g/h",
        SEED + "login",
        "https://india.gov.in/services",
        "https://facebook.com/karnataka",
    ]

    accepted = FrontierAgent.filter_links(None, links, SEED)

    assert accepted == [SEED + "about", "https://india.gov.in/services"]


def test_filter_links_never_returns_the_source_page():
    accepted = FrontierAgent.filter_links(None, [SEED, SEED + "about"], SEED)

    assert accepted == [SEED + "about"]


async def test_a_fetched_page_queues_its_own_links(store, client):
    """The regression: one batch in, new pending URLs out."""
    settings = Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).rstrip("/") == SEED.rstrip("/"):
            return httpx.Response(200, text=PAGE)
        return httpx.Response(404, text="gone")

    orchestrator = Orchestrator(
        client,
        settings,
        transport=httpx.MockTransport(handler),
    )
    try:
        await orchestrator.frontier.enqueue([SEED])
        outcome = await orchestrator.run_batch()

        assert outcome.indexed_pages == 1
        assert outcome.discovered >= 2

        pending = await client.execute(
            "SELECT url, status FROM crawl_queue WHERE status = 'pending' ORDER BY rowid"
        )
        queued = {str(row[0]) for row in pending.rows}

        assert SEED + "about" in queued
        assert SEED + "departments/health" in queued
        assert SEED + "logo.png" not in queued
        assert SEED + "login" not in queued

        # And the next batch consumes them, so the crawl keeps moving.
        second = await orchestrator.run_batch()
        assert second.attempted >= 2
    finally:
        await orchestrator.close()


async def test_discovery_stops_when_the_frontier_is_full(store, client):
    settings = Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
    )
    orchestrator = Orchestrator(client, settings)
    try:
        seeded = await orchestrator.frontier.enqueue(
            [f"https://site{index}.gov.in/" for index in range(MAX_PENDING_QUEUE + 5)]
        )
        assert seeded == MAX_PENDING_QUEUE

        added = await orchestrator.frontier.enqueue(["https://india.gov.in/extra"])
        assert added == 0
    finally:
        await orchestrator.close()


async def _mark_all_seeds_crawled(client, *, hours_ago: int) -> None:
    """Put every seed in the queue as processed, `hours_ago` hours back."""
    for seed in SEED_URLS:
        domain = seed.split("//", 1)[1].split("/", 1)[0].replace("www.", "")
        await client.execute(
            "INSERT OR REPLACE INTO crawl_queue (url, domain, status, processed_at) "
            f"VALUES (?, ?, 'processed', datetime('now', '-{hours_ago} hours'))",
            (seed, domain),
        )


async def test_refill_queues_an_unseen_seed_before_rearming(store, settings):
    frontier = FrontierAgent(store, settings)

    added = await frontier.refill(minimum=1)

    assert added == len(SEED_URLS)
    assert await frontier.depth() == len(SEED_URLS)


async def test_refill_rearms_crawled_urls_once_the_cooldown_has_elapsed(store, client, settings):
    """The stall: with every seed indexed, stage 1 finds nothing new."""
    await _mark_all_seeds_crawled(client, hours_ago=7)
    frontier = FrontierAgent(store, settings)

    added = await frontier.refill(minimum=1)

    assert added > 0
    assert await frontier.depth() == added
    pending = await client.execute("SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'")
    assert int(pending.scalar(0)) == added


async def test_refill_does_not_rearm_before_the_cooldown(store, client, settings):
    await _mark_all_seeds_crawled(client, hours_ago=0)
    frontier = FrontierAgent(store, settings)

    added = await frontier.refill(minimum=1)

    assert added == 0
    assert await frontier.depth() == 0


async def test_rearm_takes_the_stalest_rows_first(store, client, settings):
    await _mark_all_seeds_crawled(client, hours_ago=7)
    await client.execute(
        "UPDATE crawl_queue SET processed_at = datetime('now', '-30 hours') WHERE url = ?",
        (SEED_URLS[0],),
    )
    frontier = FrontierAgent(store, settings)

    await frontier.refill(minimum=1)

    first = await client.execute(
        "SELECT url FROM crawl_queue WHERE status = 'pending' ORDER BY processed_at IS NULL DESC LIMIT 1"
    )
    assert str(first.scalar("")) == SEED_URLS[0]


async def test_a_crawled_url_is_stamped_with_its_processed_time(store, client):
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status) VALUES (?, ?, 'pending')",
        (SEED, "karnataka.gov.in"),
    )
    rowid = int((await client.execute("SELECT rowid FROM crawl_queue WHERE url = ?", (SEED,))).scalar(0))

    await store.persist_batch([], [rowid])

    stamped = await client.execute("SELECT processed_at FROM crawl_queue WHERE rowid = ?", (rowid,))
    assert stamped.scalar(None) is not None
