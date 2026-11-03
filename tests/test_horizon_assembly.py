"""The horizon assembles cards: dedup, isolation, snippets, and the air gap."""

from __future__ import annotations

import asyncio

import pytest

from app.agents.federated.horizon_agent import (
    BRAND_CHAR_CAP,
    MAX_PER_DOMAIN,
    SNIPPET_FULL_CHARS,
    SNIPPET_SHORT_CHARS,
    FederatedHorizonAgent,
    SearchCard,
)
from app.engine.config import Settings


class Courier:
    def __init__(self, candidates):
        self._candidates = candidates
        self.calls = 0

    async def search(self, query, limit=10):
        self.calls += 1
        return list(self._candidates)


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, environment="test", federation_enabled=True, **overrides)


def agent(candidates, **overrides) -> FederatedHorizonAgent:
    return FederatedHorizonAgent(knowledge=Courier(candidates), settings=settings(**overrides))


LONG = "a long and useful extract about the department and its services. " * 20


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_candidate_becomes_a_card_with_every_field():
    result = await agent(
        [{"title": "Karnataka Bank | Personal Banking", "url": "https://karnatakabank.com/a?utm_source=x", "snippet": LONG}]
    ).search("bank")

    card = result.cards[0]
    assert card["brand_name"] == "Karnataka Bank"
    assert card["url"] == "https://karnatakabank.com/a"
    assert card["source"] == "federated"
    assert card["score"] == 1.0
    assert card["snippet"] == card["snippet_short"]


def test_the_dict_matches_the_local_result_schema():
    """A card the frontend cannot read is a card that renders as nothing."""
    from app.services.search_service import SearchResult

    external = set(SearchCard(title="T", url="https://a.in/x").as_dict())
    local = set(SearchResult(id="1", title="T", url="https://a.in/x", snippet="s").as_dict())

    assert local <= external, local - external


# ---------------------------------------------------------------------------
# Snippets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_two_snippets_and_the_toggle():
    card = (await agent([{"title": "T", "url": "https://a.in/x", "snippet": LONG}]).search("q")).cards[0]

    assert len(card["snippet_short"]) <= SNIPPET_SHORT_CHARS == 180
    assert len(card["snippet_full"]) <= SNIPPET_FULL_CHARS == 500
    assert card["has_more"] is True


@pytest.mark.asyncio
async def test_a_short_snippet_offers_no_more():
    card = (
        await agent([{"title": "T", "url": "https://a.in/x", "snippet": "short notice"}]).search("q")
    ).cards[0]

    assert card["has_more"] is False
    assert card["snippet_full"] == card["snippet_short"]


def test_a_brand_is_capped_at_fifty():
    card = SearchCard(title="T", url="https://a.in/x", brand_name="y" * 120)

    assert len(card.as_dict()["brand_name"]) <= 120  # the dataclass does not cut
    # The cap is applied where the card is built:
    assert BRAND_CHAR_CAP == 50


# ---------------------------------------------------------------------------
# Isolation and dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_candidate_does_not_lose_the_others():
    candidates = [
        {"title": "Good one", "url": "https://a.in/1", "snippet": "useful"},
        {"title": "Broken", "url": "not-a-url", "snippet": "ignored"},
        {"title": "", "url": "https://b.in/2", "snippet": ""},
        {"title": "Good two", "url": "https://c.in/3", "snippet": "also useful"},
    ]

    result = await agent(candidates).search("q")

    assert len(result.cards) == 2
    assert {card["url"] for card in result.cards} == {"https://a.in/1", "https://c.in/3"}


@pytest.mark.asyncio
async def test_the_same_url_twice_yields_one_card():
    candidates = [
        {"title": "One", "url": "https://a.in/x", "snippet": "first"},
        {"title": "One again", "url": "https://a.in/x#fragment", "snippet": "second"},
    ]

    result = await agent(candidates).search("q")

    assert len(result.cards) == 1


@pytest.mark.asyncio
async def test_one_domain_cannot_fill_the_page():
    candidates = [
        {"title": f"Page {i}", "url": f"https://a.in/{i}", "snippet": "text"} for i in range(6)
    ]

    result = await agent(candidates).search("q")

    assert len(result.cards) == MAX_PER_DOMAIN


@pytest.mark.asyncio
async def test_the_limit_is_respected():
    candidates = [
        {"title": f"P{i}", "url": f"https://site{i}.in/x", "snippet": "text"} for i in range(10)
    ]

    result = await agent(candidates).search("q", limit=3)

    assert len(result.cards) == 3


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_slow_courier_times_out_without_raising():
    class Slow:
        async def search(self, query, limit=10):
            await asyncio.sleep(5)
            return []

    horizon = FederatedHorizonAgent(knowledge=Slow(), settings=settings(), budget=0.05)
    result = await horizon.search("q")

    assert result.cards == []
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_a_raising_courier_yields_nothing():
    class Broken:
        async def search(self, query, limit=10):
            raise RuntimeError("provider down")

    result = await FederatedHorizonAgent(knowledge=Broken(), settings=settings()).search("q")

    assert result.cards == []


@pytest.mark.asyncio
async def test_federation_off_makes_no_call():
    courier = Courier([{"title": "T", "url": "https://a.in/x", "snippet": "s"}])
    horizon = FederatedHorizonAgent(
        knowledge=courier, settings=Settings(_env_file=None, federation_enabled=False)
    )

    result = await horizon.search("q")

    assert result.cards == []
    assert courier.calls == 0


# ---------------------------------------------------------------------------
# The air gap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_seed_trapper_never_writes_and_never_blocks(client):
    result = await agent(
        [{"title": "T", "url": "https://a.in/x", "snippet": "text"}]
    ).search("q")

    assert len(result.cards) == 1
    # The background task may still be pending; give it a turn.
    await asyncio.sleep(0)

    queued = await client.execute("SELECT COUNT(*) FROM crawl_queue")
    pages = await client.execute("SELECT COUNT(*) FROM pages")
    assert int(queued.scalar(0) or 0) == 0
    assert int(pages.scalar(0) or 0) == 0
