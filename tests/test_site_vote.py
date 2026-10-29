"""Site-level voting, and sitelinks in the search payload."""

from __future__ import annotations

import pytest

from app.engine.store import IndexStore, PageDocument
from app.indexer.site_vote import SiteVoteAgent, vote
from app.services.search_service import to_result

BODY = "karnataka health district notification office government scheme passport " * 2


def row(brand="", category="", language=""):
    return {"brand_name": brand, "category": category, "language": language}


# ---------------------------------------------------------------------------
# The vote itself
# ---------------------------------------------------------------------------


def test_the_most_common_brand_wins():
    result = vote(
        "a.gov.in",
        [row(brand="Karnataka Health"), row(brand="Karnataka Health"), row(brand="Other")],
    )

    assert result.brand == "Karnataka Health"


def test_blank_pages_do_not_vote():
    """A page with no brand is silence, not a vote for "no brand"."""
    result = vote(
        "a.gov.in",
        [row(brand="Karnataka Health"), row(brand=""), row(brand="")],
    )

    assert result.brand == "Karnataka Health"


def test_a_tie_goes_to_the_longer_name():
    """A full publisher name beats the abbreviation of the same one."""
    result = vote("a.gov.in", [row(brand="Karnataka"), row(brand="Karnataka Health")])

    assert result.brand == "Karnataka Health"


def test_the_dominant_category_and_language_win():
    result = vote(
        "a.gov.in",
        [
            row(category="government", language="kn"),
            row(category="government", language="kn"),
            row(category="news_media", language="en"),
        ],
    )

    assert result.category == "government"
    assert result.language == "kn"


def test_an_empty_domain_votes_for_nothing():
    result = vote("a.gov.in", [])

    assert result.brand == ""
    assert result.pages == 0


# ---------------------------------------------------------------------------
# Applying the vote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_consensus_is_written_across_the_domain(client, settings):
    store = IndexStore(client, settings)
    await store.persist_batch(
        [
            PageDocument(
                url=f"https://a.gov.in/p{i}", domain="a.gov.in", title=f"Page {i}",
                description="d", body_text=BODY, digest=f"v{i}",
                brand_name="Karnataka Health" if i < 2 else "",
                category="government" if i < 2 else "general_reference",
                language="kn" if i < 2 else "en",
            )
            for i in range(3)
        ]
    )

    agent = SiteVoteAgent(client, min_pages=3)
    votes = await agent.scan()
    report = await agent.apply(votes)

    assert report.domains_voted == 1
    stored = await client.execute(
        "SELECT DISTINCT brand_name, category, language FROM pages"
    )
    assert len(stored.rows) == 1
    assert list(stored.rows[0]) == ["Karnataka Health", "government", "kn"]


@pytest.mark.asyncio
async def test_a_domain_with_too_few_pages_is_not_voted_on(client, settings):
    """Two pages is not a consensus, it is a coincidence."""
    store = IndexStore(client, settings)
    await store.persist_batch(
        [
            PageDocument(
                url=f"https://b.gov.in/p{i}", domain="b.gov.in", title="T", description="d",
                body_text=BODY, digest=f"w{i}", brand_name="B",
            )
            for i in range(2)
        ]
    )

    assert await SiteVoteAgent(client, min_pages=3).scan() == []


# ---------------------------------------------------------------------------
# Sitelinks in the payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sitelinks_reach_the_search_payload(client, settings):
    store = IndexStore(client, settings)
    await store.persist_batch(
        [
            PageDocument(
                url="https://karnataka.gov.in/apply", domain="karnataka.gov.in",
                title="Apply online", description="d", body_text=BODY, digest="sl-1",
                brand_name="Karnataka",
                sitelinks=[
                    {"intent": "apply", "label": "Apply Online",
                     "url": "https://karnataka.gov.in/apply"},
                    {"intent": "contact", "label": "Contact Us",
                     "url": "https://karnataka.gov.in/contact"},
                ],
            )
        ]
    )

    hits = await store.search("karnataka health")
    payload = to_result(hits[0]).as_dict()

    assert len(payload["sitelinks"]) == 2
    assert payload["sitelinks"][0]["intent"] == "apply"


@pytest.mark.asyncio
async def test_a_page_with_no_sitelinks_returns_an_empty_list(client, settings):
    """Backward compatible: the field is always present, never null."""
    store = IndexStore(client, settings)
    await store.persist_batch(
        [
            PageDocument(
                url="https://b.gov.in/plain", domain="b.gov.in", title="Plain",
                description="d", body_text=BODY, digest="sl-2", brand_name="B",
            )
        ]
    )

    hits = await store.search("karnataka health")
    payload = to_result(hits[0]).as_dict()

    assert payload["sitelinks"] == []
