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

# These live in the brand agent now; the parser re-exports the two it uses.
brand_module = load_agent("10_brand_canonical_agent")
action_title_from = brand_module.action_title_from
brand_from_domain = brand_module.brand_from_domain
clean_url_for = brand_module.clean_url_for


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


def test_the_action_title_is_capped_at_five_words_and_sixty_characters():
    resolved = action_title_from("Apply for a new passport online today right now")
    assert resolved == "Apply for a new passport"

    long_words = "Supercalifragilisticexpialidocious" * 2
    assert len(action_title_from(long_words)) <= 60


def test_a_short_title_is_not_padded_out():
    assert action_title_from("District Health Notifications") == "District Health Notifications"


def test_the_action_title_prefers_og_title_over_the_h1_and_title():
    assert (
        action_title_from(
            "Raw title | Site",
            h1="Heading on the page",
            og_title="Running Shoes Online",
        )
        == "Running Shoes Online"
    )


def test_the_action_title_uses_the_h1_only_when_it_is_longer_than_five_characters():
    # "Help" is four characters, so it is not a usable title on its own.
    assert action_title_from("Passport Application", h1="Help") == "Passport Application"
    assert action_title_from("Raw | Site", h1="Passport Renewal") == "Passport Renewal"


def test_the_action_title_drops_the_brand_it_already_shows_elsewhere():
    """The card prints the brand on its own line, so the title must not repeat it."""
    assert action_title_from("Running Shoes Online | Amazon.in", brand="Amazon.in") == "Running Shoes Online"
    assert action_title_from("Amazon.in | Running Shoes Online", brand="Amazon.in") == "Running Shoes Online"


def test_the_action_title_drops_a_part_that_is_only_the_site_root():
    assert action_title_from("Karnataka news - thehindu", url="https://thehindu.com/news") == "Karnataka news"


def test_the_action_title_falls_back_to_the_url_slug():
    """A page whose title is useless still gets a readable label."""
    assert action_title_from("Home", url="https://example.com/apply-for-a-passport") == "apply for a passport"


def test_the_slug_fallback_skips_noise_segments():
    assert action_title_from("", url="https://example.com/index.html") == ""
    assert action_title_from("", url="https://example.com/news/2026/budget-2026.html") == "budget 2026"


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


# ---------------------------------------------------------------------------
# The categories table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_categories_table_is_seeded(client):
    result = await client.execute("SELECT code, name FROM categories ORDER BY id")
    rows = {code: name for code, name in result.rows}
    assert rows["GOV"] == "government"
    assert rows["WEB"] == "general_reference"
    # Every category the classifier can emit must have a row, or a page would
    # link to nothing.
    assert {"GOV", "TECH", "NEWS", "JOBS", "FINANCE", "EDU", "HEALTH", "AGRI", "LEGAL", "WEB"} <= set(rows)


@pytest.mark.asyncio
async def test_a_written_page_is_linked_to_its_category(client):
    await client.execute(
        "INSERT INTO pages (url, title, body_text, digest, category) "
        "VALUES ('https://karnataka.gov.in/a', 'Health', 'text', 'cat-1', 'government')"
    )
    joined = await client.execute(
        "SELECT p.category, c.code FROM pages p "
        "LEFT JOIN categories c ON c.id = p.category_id WHERE p.url = 'https://karnataka.gov.in/a'"
    )
    assert joined.rows == [["government", "GOV"]]


@pytest.mark.asyncio
async def test_an_unknown_category_still_links_to_web(client):
    """A category the table has never heard of must not leave the row unlinked."""
    await client.execute(
        "INSERT INTO pages (url, title, body_text, digest, category) "
        "VALUES ('https://x.example/a', 'T', 'text', 'cat-2', 'something_new')"
    )
    joined = await client.execute(
        "SELECT c.code FROM pages p LEFT JOIN categories c ON c.id = p.category_id "
        "WHERE p.url = 'https://x.example/a'"
    )
    assert joined.scalar(0) == "WEB"
