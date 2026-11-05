"""The blended pipeline, end to end through SearchService."""

from __future__ import annotations

import asyncio

import pytest

from app.agents.federated.horizon_agent import SearchCard
from app.agents.federated.knowledge_blender import DEFAULT_TIMEOUT_MS
from app.engine.config import Settings
from app.engine.store import PageDocument
from app.services.search_service import SearchService

BODY = "a full paragraph of genuine content about the department and its services. " * 6


def settings(**overrides) -> Settings:
    base = dict(environment="test", federation_enabled=True, search_cache_ttl_seconds=0)
    base.update(overrides)
    return Settings(_env_file=None, **base)


class Fleet:
    def __init__(self, cards=None, *, delay=0.0, boom=False):
        self._cards = cards or []
        self._delay = delay
        self._boom = boom
        self.calls = 0

    async def search(self, query, *, limit=10):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._boom:
            raise RuntimeError("provider exploded")
        return [card.as_dict() for card in self._cards][:limit]


def page(url: str, title: str, digest: str) -> PageDocument:
    return PageDocument(
        url=url, domain=url.split("/")[2], title=title, description="d",
        body_text=BODY, digest=digest, brand_name="A",
    )


# ---------------------------------------------------------------------------
# Both legs together
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_organic_results_and_cards_are_both_returned(store):
    await store.persist_batch([page("https://karnataka.gov.in/x", "Karnataka State Portal", "bi-1")])
    fleet = Fleet([SearchCard(title="Karnataka history overview", url="https://en.wikipedia.org/wiki/Karnataka", snippet_short="Karnataka is a state in south western India.")])

    service = SearchService(store, settings())
    service._federation = fleet
    payload = await service.search("karnataka state")

    assert payload["results"], "the crawled page must be present"
    assert payload["knowledge_cards"], "the federated card must be present"
    assert payload["results"][0]["url"] == "https://karnataka.gov.in/x"


@pytest.mark.asyncio
async def test_results_remains_the_organic_list(store):
    """A consumer that only reads `results` sees what it saw before."""
    await store.persist_batch([page("https://karnataka.gov.in/x", "Karnataka State Portal", "bi-2")])
    service = SearchService(store, settings())
    service._federation = Fleet([SearchCard(title="External", url="https://a.in/y", snippet_short="text")])

    payload = await service.search("karnataka state")

    assert all(result["url"].startswith("https://karnataka.gov.in") for result in payload["results"])


@pytest.mark.asyncio
async def test_the_new_keys_are_present_and_additive(store):
    service = SearchService(store, settings())
    service._federation = Fleet([])

    payload = await service.search("anything-at-all")

    for key in ("results", "total_estimate", "page", "limit", "took_ms"):
        assert key in payload, key
    assert "knowledge_cards" in payload
    assert "primary_card" in payload
    assert "blend" in payload


# ---------------------------------------------------------------------------
# Budget and failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_slow_fleet_costs_cards_not_results(store):
    await store.persist_batch([page("https://karnataka.gov.in/x", "Karnataka State Portal", "bi-3")])
    service = SearchService(store, settings())
    service._federation = Fleet(
        [SearchCard(title="Late", url="https://a.in/y", snippet_short="text")], delay=5
    )

    payload = await service.search("karnataka state")

    assert payload["results"], "organic results survive a federated timeout"
    assert payload["knowledge_cards"] == []
    assert payload["blend"]["degraded"] is True


@pytest.mark.asyncio
async def test_a_failing_fleet_never_raises(store):
    await store.persist_batch([page("https://karnataka.gov.in/x", "Karnataka State Portal", "bi-4")])
    service = SearchService(store, settings())
    service._federation = Fleet(boom=True)

    payload = await service.search("karnataka state")

    assert payload["results"]
    assert payload["knowledge_cards"] == []


@pytest.mark.asyncio
async def test_the_budget_is_the_blenders(store):
    """One budget, two places that name it. They must agree."""
    from app.services import search_service

    assert DEFAULT_TIMEOUT_MS == search_service.FEDERATION_TIMEOUT_MS


@pytest.mark.asyncio
async def test_federation_off_makes_no_call(store):
    fleet = Fleet([SearchCard(title="X", url="https://a.in/y", snippet_short="s")])
    service = SearchService(store, settings(federation_enabled=False))
    service._federation = fleet

    payload = await service.search("anything-at-all")

    assert fleet.calls == 0
    assert payload["knowledge_cards"] == []


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_navigational_query_keeps_the_crawled_page_first(store):
    await store.persist_batch([page("https://karnatakabank.com/personal", "Karnataka Bank Personal Banking", "bi-5")])
    service = SearchService(store, settings())
    service._federation = Fleet(
        [SearchCard(title="Karnataka Bank history page", url="https://en.wikipedia.org/wiki/Karnataka_Bank", snippet_short="Karnataka Bank is an Indian bank.")]
    )

    payload = await service.search("karnataka bank")

    assert payload["results"][0]["url"].startswith("https://karnatakabank.com")
    assert payload["primary_card"] is None
    assert payload["blend"]["intent"] == "navigational"


@pytest.mark.asyncio
async def test_an_informational_query_can_promote_a_hero_card(store):
    service = SearchService(store, settings())
    service._federation = Fleet(
        [SearchCard(
            title="Karnataka",
            url="https://en.wikipedia.org/wiki/Karnataka",
            snippet_short="Karnataka is a state in south western India with Bengaluru as its capital city and a long recorded history.",
        )]
    )

    payload = await service.search("what is karnataka")

    assert payload["primary_card"] is not None
    assert payload["primary_card"]["title"] == "Karnataka"
    assert payload["blend"]["intent"] == "informational"


@pytest.mark.asyncio
async def test_a_card_pointing_at_a_crawled_page_is_not_repeated(store):
    await store.persist_batch([page("https://karnataka.gov.in/x", "Karnataka State Portal", "bi-6")])
    service = SearchService(store, settings())
    service._federation = Fleet(
        [SearchCard(title="Karnataka State Portal", url="https://karnataka.gov.in/x", snippet_short="the same page")]
    )

    payload = await service.search("karnataka state")

    assert payload["knowledge_cards"] == []
