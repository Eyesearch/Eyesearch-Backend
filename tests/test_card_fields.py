"""The card-oriented fields: brand, breadcrumb and action title.

These exist because a raw <title> and a bare URL make a poor result card. Portals
stuff their titles with pipes and keywords, and a URL path is not a breadcrumb a
reader can scan.
"""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.engine.config import Settings
from app.engine.store import IndexStore, PageDocument
from app.services.search_service import to_result

parser_module = load_agent("03_parser_agent")
ParserAgent = parser_module.ParserAgent
action_title_from = parser_module.action_title_from
brand_from_domain = parser_module.brand_from_domain
clean_url_for = parser_module.clean_url_for


PAGE = """<html><head>
<meta property="og:site_name" content="Karnataka Health Department">
<title>Notifications | Karnataka Health Department | Apply Online | Official Website</title>
<meta name="description" content="District health notifications and application deadlines for every district office.">
</head><body>
<h1>District Health Notifications</h1>
<p>The department publishes district health notifications for every office across the state.</p>
</body></html>"""


def test_the_brand_comes_from_og_site_name():
    parsed = ParserAgent().parse("https://www.karnataka.gov.in/health", PAGE)
    assert parsed.brand_name == "Karnataka Health Department"


def test_the_brand_comes_from_json_ld_when_there_is_no_og_tag():
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type": "NewsArticle", "publisher": {"name": "The Hindu"}}'
        "</script><title>Karnataka news</title></head><body><p>Some copy here.</p></body></html>"
    )
    parsed = ParserAgent().parse("https://thehindu.com/news", html)
    assert parsed.brand_name == "The Hindu"


def test_the_brand_falls_back_to_the_domain_root():
    assert brand_from_domain("https://karnataka.gov.in/x") == "Karnataka"
    assert brand_from_domain("https://www.example.com/") == "Example"


def test_the_brand_falls_back_to_nothing_for_an_unparsable_url():
    assert brand_from_domain("not a url") == ""


def test_the_clean_url_reads_as_a_breadcrumb():
    assert (
        clean_url_for("https://www.karnataka.gov.in/health/notifications/index.html")
        == "karnataka.gov.in › health › notifications"
    )


def test_the_clean_url_drops_noise_segments_and_keeps_the_host():
    assert clean_url_for("https://example.com/index.html") == "example.com"
    assert clean_url_for("https://example.com/en/apply-online") == "example.com › apply online"


def test_the_clean_url_strips_www():
    assert clean_url_for("https://www.thehindu.com/news/").startswith("thehindu.com")


def test_the_action_title_prefers_the_h1():
    parsed = ParserAgent().parse("https://karnataka.gov.in/health", PAGE)
    assert parsed.action_title == "District Health Notifications"


def test_the_action_title_strips_pipe_stuffing_when_there_is_no_h1():
    html = (
        "<html><head><title>Passport Application | Apply Online | Fees | "
        "Official Portal</title></head><body><p>Apply for a passport.</p></body></html>"
    )
    parsed = ParserAgent().parse("https://passportindia.gov.in/apply", html)
    assert parsed.action_title == "Passport Application"


def test_the_action_title_strips_dash_stuffing():
    assert action_title_from("Income Tax Return Filing - File Online - Department") == "Income Tax Return Filing"


def test_the_action_title_is_capped_at_four_words():
    assert action_title_from("Apply for a new passport online today") == "Apply for a new"


def test_the_action_title_skips_a_title_that_is_only_the_brand():
    assert action_title_from("The Hindu | Karnataka news", brand="The Hindu") == "Karnataka news"


def test_the_body_text_does_not_include_the_head():
    """`soup.get_text()` returns <title> and <meta> too, which put the stuffed
    title at the front of every snippet."""
    parsed = ParserAgent().parse("https://karnataka.gov.in/health", PAGE)
    assert "Apply Online" not in parsed.body_text
    assert parsed.body_text.startswith("District Health Notifications The department")


@pytest.mark.asyncio
async def test_the_api_result_carries_the_card_fields(client):
    parsed = ParserAgent().parse(
        "https://www.karnataka.gov.in/health/notifications/index.html", PAGE
    )
    store = IndexStore(client, Settings(environment="test"))
    await store.persist_batch(
        [
            PageDocument(
                url=parsed.url,
                domain="karnataka.gov.in",
                title=parsed.title,
                description=parsed.description,
                body_text=parsed.body_text,
                digest="card-1",
                brand_name=parsed.brand_name,
                clean_url=parsed.clean_url,
                action_title=parsed.action_title,
            )
        ]
    )

    hits = await store.search("health notifications")
    assert hits, "the seeded page should be found"
    result = to_result(hits[0]).as_dict()

    assert result["title"] == "District Health Notifications"
    assert result["brand_name"] == "Karnataka Health Department"
    assert result["clean_url"] == "karnataka.gov.in › health › notifications"


@pytest.mark.asyncio
async def test_a_row_without_an_action_title_falls_back_to_its_title(client):
    """Rows indexed before this field existed must still render."""
    store = IndexStore(client, Settings(environment="test"))
    await store.persist_batch(
        [
            PageDocument(
                url="https://legacy.gov.in/old",
                domain="legacy.gov.in",
                title="Old page title",
                description="An older row",
                body_text="An older row that predates the card fields.",
                digest="card-2",
            )
        ]
    )
    hits = await store.search("older row")
    assert hits
    result = to_result(hits[0]).as_dict()
    assert result["title"] == "Old page title"
    assert result["brand_name"] == ""
    assert result["clean_url"] == ""
