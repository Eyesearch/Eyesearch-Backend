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
