"""Per-domain page cap and page-specific descriptions.

Two problems with one cause: a large portal dominating the index.

- Nothing bounded how many pages one domain could contribute, so a single
  railway or government portal could crowd out every other source.
- Portals ship one boilerplate meta description across hundreds of subpages, so
  every result card read identically.
"""

from __future__ import annotations

import httpx
import pytest

from app.agents import load_agent
from app.engine.config import Settings
from app.engine.store import PageDocument
from app.orchestrator import Orchestrator

parser_module = load_agent("03_parser_agent")

DESCRIPTION_CHAR_CAP = parser_module.DESCRIPTION_CHAR_CAP
DESCRIPTION_SNIPPET_CHARS = parser_module.DESCRIPTION_SNIPPET_CHARS
ELLIPSIS = parser_module.ELLIPSIS
ParserAgent = parser_module.ParserAgent
build_description = parser_module.build_description
is_generic_description = parser_module.is_generic_description

BODY = (
    "Purple Line services run from 05:00 to 23:00 on weekdays. The Bengaluru Metro "
    "Purple Line connects Kengeri to Whitefield, and fares are charged by distance."
)
GENERIC = "Official portal of the Government of Karnataka."


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------


def test_a_boilerplate_description_is_replaced_with_page_specific_text():
    description = build_description("Purple Line timings", GENERIC, BODY)

    assert description.startswith("Purple Line timings")
    assert "05:00 to 23:00" in description
    assert GENERIC not in description


def test_a_specific_description_is_kept_verbatim():
    specific = "Timings and fares for the Purple Line."

    assert build_description("Purple Line timings", specific, BODY) == specific


def test_a_missing_description_is_rebuilt_from_the_body():
    description = build_description("Purple Line timings", "", BODY)

    assert description.startswith("Purple Line timings")
    assert "Purple Line services" in description


def test_a_description_that_only_repeats_the_title_is_generic():
    title = "Karnataka State Portal"

    assert is_generic_description(title, title) is True
    assert build_description(title, title, BODY).startswith(title)


@pytest.mark.parametrize(
    "meta",
    [
        "Official portal of the Government of Karnataka.",
        "Official Website of Indian Railways",
        "Welcome to the district portal",
        "Home page",
        "too short",
    ],
)
def test_recognised_boilerplate_openings_are_generic(meta):
    assert is_generic_description(meta, "Some Page Title") is True


def test_the_description_never_exceeds_its_cap():
    long_body = " ".join(["karnataka"] * 200)

    description = build_description("A" * 200, GENERIC, long_body)

    assert len(description) <= DESCRIPTION_CHAR_CAP


def test_no_ellipsis_is_added_when_nothing_was_cut():
    short_body = "Purple Line services run from 05:00 to 23:00."

    description = build_description("Timings", GENERIC, short_body)

    assert ELLIPSIS not in description


def test_an_ellipsis_marks_a_body_that_was_cut():
    long_body = " ".join(["karnataka"] * 200)

    description = build_description("Timings", GENERIC, long_body)

    assert ELLIPSIS in description
    assert len(description.split(ELLIPSIS)[0].split(" — ")[1]) <= DESCRIPTION_SNIPPET_CHARS


def test_two_subpages_of_one_portal_get_different_descriptions():
    """The actual complaint: cloned meta descriptions across subpages."""
    parser = ParserAgent()
    first = f"""<html><head><title>Metro timings</title>
        <meta name="description" content="{GENERIC}"></head>
        <body><p>{BODY}</p></body></html>"""
    second = f"""<html><head><title>Bus fares</title>
        <meta name="description" content="{GENERIC}"></head>
        <body><p>KSRTC bus fares are fixed by route and class of service,
        and season tickets are available at every depot counter.</p></body></html>"""

    a = parser.parse("https://karnataka.gov.in/metro", first)
    b = parser.parse("https://karnataka.gov.in/bus", second)

    assert a.description != b.description
    assert a.description.startswith("Metro timings")
    assert b.description.startswith("Bus fares")


# ---------------------------------------------------------------------------
# Per-domain cap
# ---------------------------------------------------------------------------


def _page(domain: str, index: int) -> PageDocument:
    return PageDocument(
        url=f"https://{domain}/page-{index}",
        domain=domain,
        title=f"Page {index}",
        description=f"Page {index} of {domain}.",
        body_text=f"Page {index} of {domain} carries its own distinct prose about the subject.",
        digest=f"{domain}-{index}",
    )


async def test_domain_page_counts_come_back_in_one_query(store, client):
    await store.persist_batch(
        [_page("karnataka.gov.in", index) for index in range(3)]
        + [_page("india.gov.in", index) for index in range(2)]
    )

    counts = await store.domain_page_counts(["karnataka.gov.in", "india.gov.in", "other.gov.in"])

    assert counts == {"karnataka.gov.in": 3, "india.gov.in": 2}


async def test_a_full_domain_is_skipped_rather_than_crawled(store, client):
    settings = Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
        max_pages_per_domain=2,
    )
    await store.persist_batch([_page("karnataka.gov.in", index) for index in range(2)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><head><title>Another page</title></head>"
            "<body><p>A full page of prose about the department and its services.</p></body></html>",
        )

    orchestrator = Orchestrator(client, settings, transport=httpx.MockTransport(handler))
    try:
        await orchestrator.frontier.enqueue(["https://karnataka.gov.in/page-99"])
        outcome = await orchestrator.run_batch()

        # A capped domain is excluded at dequeue, so its URL never reaches the
        # batch at all — no wasted fetch, no wasted slot.
        assert outcome.attempted == 0
        assert outcome.fetched == 0
        assert outcome.indexed_pages == 0

        # The row is parked by the reconciliation, not by the batch.
        await store.skip_pending_for_capped_domains(cap=2)
        status = await client.execute(
            "SELECT status FROM crawl_queue WHERE url = ?",
            ("https://karnataka.gov.in/page-99",),
        )
        assert status.scalar("") == "skipped"
    finally:
        await orchestrator.close()


async def test_the_cap_holds_when_a_domain_fills_up_mid_batch(store, client):
    settings = Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
        max_pages_per_domain=2,
    )
    await store.persist_batch([_page("karnataka.gov.in", 0)])

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        return httpx.Response(
            200,
            text=f"<html><head><title>{url}</title></head>"
            f"<body><p>Distinct prose for {url} covering the department and its services.</p></body></html>",
        )

    orchestrator = Orchestrator(client, settings, transport=httpx.MockTransport(handler))
    try:
        await orchestrator.frontier.enqueue(
            [
                "https://karnataka.gov.in/new-a",
                "https://karnataka.gov.in/new-b",
                "https://karnataka.gov.in/new-c",
            ]
        )
        outcome = await orchestrator.run_batch()

        # One slot was left, so exactly one page is accepted and two are skipped.
        assert outcome.indexed_pages == 1
        assert outcome.skipped == 2

        total = await client.execute(
            "SELECT COUNT(*) FROM pages p JOIN sites s ON s.site_id = p.site_id "
            "WHERE s.domain = 'karnataka.gov.in'"
        )
        assert int(total.scalar(0)) == 2
    finally:
        await orchestrator.close()


async def test_a_capped_domain_does_not_grow_the_frontier(store, client):
    settings = Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
        max_pages_per_domain=1,
    )
    await store.persist_batch([_page("karnataka.gov.in", 0)])

    html = (
        "<html><head><title>Portal</title></head><body>"
        '<a href="/one">1</a><a href="/two">2</a><a href="/three">3</a>'
        "<p>A full page of prose about the department and its services.</p></body></html>"
    )

    orchestrator = Orchestrator(
        client, settings, transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html))
    )
    try:
        await orchestrator.frontier.enqueue(["https://karnataka.gov.in/start"])
        outcome = await orchestrator.run_batch()

        # The capped domain is not even dequeued, so it contributes nothing.
        assert outcome.attempted == 0
        assert outcome.discovered == 0
    finally:
        await orchestrator.close()


async def test_skipped_rows_are_never_rearmed(store, client, settings):
    await client.execute(
        "INSERT INTO crawl_queue (url, domain, status, processed_at) "
        "VALUES ('https://karnataka.gov.in/x', 'karnataka.gov.in', 'skipped', "
        "datetime('now', '-30 days'))"
    )

    rearmed = await store.rearm_processed(limit=50, cooldown_seconds=0)

    assert rearmed == 0
    status = await client.execute(
        "SELECT status FROM crawl_queue WHERE url = 'https://karnataka.gov.in/x'"
    )
    assert status.scalar("") == "skipped"


async def test_the_cap_is_configurable(store, client):
    settings = Settings(environment="test", max_pages_per_domain=25)
    orchestrator = Orchestrator(client, settings)
    try:
        assert orchestrator._settings.max_pages_per_domain == 25
    finally:
        await orchestrator.close()


def test_the_default_cap_is_twenty_five_pages():
    assert Settings().max_pages_per_domain == 25


async def test_a_page_under_the_cap_is_still_crawled(store, client):
    settings = Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
        max_pages_per_domain=3,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><head><title>Fresh page</title></head>"
            "<body><p>This department publishes notifications, citizen services and "
            "contact details for every district office across the state.</p></body></html>",
        )

    orchestrator = Orchestrator(client, settings, transport=httpx.MockTransport(handler))
    try:
        await orchestrator.frontier.enqueue(["https://new.gov.in/"])
        outcome = await orchestrator.run_batch()

        assert outcome.indexed_pages == 1
        assert outcome.skipped == 0
    finally:
        await orchestrator.close()


async def test_the_cap_is_shared_across_subdomains_of_one_site(store, client):
    """The loophole: ten pages per subdomain instead of ten per site."""
    settings = Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
        max_pages_per_domain=3,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        return httpx.Response(
            200,
            text=f"<html><head><title>{url}</title></head>"
            f"<body><p>Distinct prose for {url} covering the project and its documentation.</p></body></html>",
        )

    orchestrator = Orchestrator(client, settings, transport=httpx.MockTransport(handler))
    try:
        await orchestrator.frontier.enqueue(
            [
                "https://python.fossee.in/guide",
                "https://arduino.fossee.in/guide",
                "https://om.fossee.in/guide",
                "https://scilab.fossee.in/guide",
                "https://spoken-tutorial.fossee.in/guide",
            ]
        )
        outcome = await orchestrator.run_batch()

        assert outcome.indexed_pages == 3
        assert outcome.skipped == 2

        total = await client.execute(
            "SELECT COUNT(*) FROM pages p JOIN sites s ON s.site_id = p.site_id "
            "WHERE s.domain = 'fossee.in'"
        )
        assert int(total.scalar(0)) == 3
    finally:
        await orchestrator.close()


async def test_subdomains_share_one_round_robin_slot(store, client):
    """One site's subdomains must not crowd the batch as separate domains."""
    settings = Settings(environment="test", libsql_min_interval_seconds=0.0)
    orchestrator = Orchestrator(client, settings)
    try:
        await orchestrator.frontier.enqueue(
            [f"https://sub{i}.fossee.in/p" for i in range(6)]
            + ["https://www.thehindu.com/news/", "https://indiankanoon.org/doc/1"]
        )

        batch = await store.pending_batch(10)
        domains = [item.domain for item in batch]

        assert "fossee.in" in domains
        assert "thehindu.com" in domains
        # The first three slots are three sites, not three subdomains of one.
        assert len(set(domains[:3])) == 3
    finally:
        await orchestrator.close()
