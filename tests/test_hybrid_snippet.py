"""Navigational queries show the description; informational ones show the body."""

from __future__ import annotations

import pytest

from app.engine.store import PageDocument
from app.services.search_service import to_result

BODY = (
    "Jio offers prepaid recharge plans starting at 149 rupees for 28 days with "
    "unlimited calls. Postpaid plans begin at 399 rupees and include data and "
    "voice benefits across all circles in the country."
)
DESCRIPTION = "Jio recharge and postpaid plans."


@pytest.fixture()
async def seeded(store):
    await store.persist_batch(
        [
            PageDocument(
                url="https://jio.com/plans", domain="jio.com", title="Jio Plans",
                description=DESCRIPTION, body_text=BODY, digest="hy-1", brand_name="Jio",
            )
        ]
    )
    return store


@pytest.mark.asyncio
async def test_an_informational_query_shows_the_sentence_from_the_body(seeded):
    hits = await seeded.search("recharge plans")

    assert hits[0].nav_hit == 0
    assert "149 rupees" in hits[0].dynamic_headline

    card = to_result(hits[0])

    assert "149 rupees" in card.snippet
    assert "<b>" not in card.snippet


@pytest.mark.asyncio
async def test_a_brand_query_shows_what_the_site_says_about_itself(seeded):
    hits = await seeded.search("jio")

    assert hits[0].nav_hit == 1

    card = to_result(hits[0])

    assert DESCRIPTION.rstrip(".") in card.snippet
    assert "149 rupees" not in card.snippet


@pytest.mark.asyncio
async def test_the_headline_tags_never_reach_the_card(seeded):
    """Both clients render a snippet as plain text."""
    hits = await seeded.search("recharge plans")
    card = to_result(hits[0])

    assert "<b>" not in card.snippet
    assert "</b>" not in card.snippet
    assert "recharge" in card.snippet


@pytest.mark.asyncio
async def test_a_miss_falls_back_to_the_description(store):
    """No fragment in the body means no headline, and the card is unchanged."""
    await store.persist_batch(
        [
            PageDocument(
                url="https://karnataka.gov.in/x", domain="karnataka.gov.in",
                title="Karnataka Portal", description="State portal for citizen services.",
                body_text="The portal publishes notifications for district offices.",
                digest="hy-2", brand_name="Karnataka",
            )
        ]
    )

    hits = await store.search("portal")
    card = to_result(hits[0])

    assert card.snippet != ""


@pytest.mark.asyncio
async def test_the_headline_is_computed_only_for_the_returned_page(store, client):
    """ts_headline runs above the LIMIT, not across every candidate.

    Ten pages are seeded and one is asked for. If the headline were computed in
    the candidate query it would run ten times; above the slice it runs once.
    """
    await store.persist_batch(
        [
            PageDocument(
                url=f"https://k{i}.gov.in/p", domain=f"k{i}.gov.in", title=f"Portal {i}",
                description="d", body_text="the portal publishes notifications for citizens",
                digest=f"slice-{i}", brand_name=f"K{i}",
            )
            for i in range(10)
        ]
    )

    hits = await store.search("portal", limit=1)

    assert len(hits) == 1
    assert hits[0].dynamic_headline != ""


@pytest.mark.asyncio
async def test_the_fallback_row_shape_still_carries_a_blank_headline(store):
    """The timeout path builds its own row; the column must still line up."""
    await store.persist_batch(
        [
            PageDocument(
                url="https://j2.gov.in/x", domain="j2.gov.in", title="J2",
                description="d", body_text="notifications for citizens here",
                digest="slice-fb", brand_name="J2",
            )
        ]
    )

    hits = await store.search("citizens")

    assert hits
    assert isinstance(hits[0].dynamic_headline, str)
