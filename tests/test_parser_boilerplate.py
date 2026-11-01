"""Boilerplate that must never reach the body, and the sweep that repairs rows."""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.engine.store import PageDocument
from app.indexer import maintenance

parser = load_agent("03_parser_agent")

PAGE = """<html><body>
<header><nav>Skip to main content Follow Us on Twitter.</nav></header>
<main>
  <h1>National Eligibility Test</h1>
  <p>The National Eligibility Test is conducted to determine the eligibility of
  Indian nationals for Assistant Professorship and Junior Research Fellowship
  across the country every year.</p>
</main>
<footer>Copyright 2026 Government of India All Rights Reserved</footer>
</body></html>"""


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def test_structural_tags_are_stripped():
    tags = set(parser.STRIP_TAGS)

    for tag in ("header", "nav", "footer", "aside", "form", "button"):
        assert tag in tags, tag


def test_the_accessibility_preamble_does_not_reach_the_body():
    body = parser.ParserAgent().parse("https://nta.ac.in/x", PAGE).body_text

    assert "Skip to" not in body
    assert "Follow Us" not in body


def test_the_footer_copyright_does_not_reach_the_body():
    body = parser.ParserAgent().parse("https://nta.ac.in/x", PAGE).body_text

    assert "All Rights Reserved" not in body


def test_the_main_content_survives():
    body = parser.ParserAgent().parse("https://nta.ac.in/x", PAGE).body_text

    assert "Assistant Professorship" in body


@pytest.mark.parametrize(
    "opening",
    [
        "Skip to main content The real text starts here.",
        "Skip to content The real text starts here.",
        "Screen reader access The real text starts here.",
        "Follow us on Twitter. The real text starts here.",
        "Home / About Us / Contact / The real text starts here.",
    ],
)
def test_a_leading_junk_phrase_is_removed(opening):
    assert parser.clean_leading_boilerplate(opening).startswith("The real text")


def test_the_same_words_later_in_the_body_are_prose():
    """Only the front is scrubbed. A notice may legitimately say this."""
    text = "The department asks applicants to follow us on Twitter for updates."

    assert parser.clean_leading_boilerplate(text) == text


def test_a_small_article_wrapper_does_not_beat_a_full_body():
    """Templated sites wrap an empty <article> around nothing."""
    html = (
        "<html><body><article> </article>"
        "<p>" + "Real content that is long enough to matter. " * 6 + "</p>"
        "</body></html>"
    )

    body = parser.ParserAgent().parse("https://a.example/x", html).body_text

    assert "Real content" in body


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _page(url: str, body: str, digest: str) -> PageDocument:
    return PageDocument(
        url=url, domain="a.gov.in", title="T", description="d",
        body_text=body, digest=digest, brand_name="A",
    )


LONG = "a full paragraph of genuine content worth keeping. " * 8


@pytest.mark.asyncio
async def test_the_sweep_finds_short_and_junk_opened_rows(client, store):
    await store.persist_batch(
        [
            _page("https://a.gov.in/short", "Post Docto", "bp-1"),
            _page("https://a.gov.in/junk", "Skip to main content. " + LONG, "bp-2"),
            _page("https://a.gov.in/good", LONG, "bp-3"),
        ]
    )

    assert await maintenance.sweep_and_requeue_legacy_pages(client, dry_run=True) == 2


@pytest.mark.asyncio
async def test_a_dry_run_queues_nothing(client, store):
    await store.persist_batch([_page("https://a.gov.in/short", "tiny", "bp-4")])

    await maintenance.sweep_and_requeue_legacy_pages(client, dry_run=True)

    queued = await client.execute("SELECT COUNT(*) FROM crawl_queue")
    assert int(queued.scalar(0) or 0) == 0


@pytest.mark.asyncio
async def test_the_sweep_queues_the_candidates_at_repair_priority(client, store):
    await store.persist_batch([_page("https://a.gov.in/short", "tiny", "bp-5")])

    await maintenance.sweep_and_requeue_legacy_pages(client)

    row = await client.execute("SELECT priority, status FROM crawl_queue")
    priority, status = row.rows[0]
    assert int(priority) == maintenance.REPAIR_PRIORITY
    assert status == "pending"


@pytest.mark.asyncio
async def test_a_healthy_row_is_left_alone(client, store):
    await store.persist_batch([_page("https://a.gov.in/good", LONG, "bp-6")])

    assert await maintenance.sweep_and_requeue_legacy_pages(client) == 0
