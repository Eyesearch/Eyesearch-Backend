"""The fallback path: only on empty, bounded, and never a 500."""

from __future__ import annotations

import asyncio

import pytest

from app.agents.federated.horizon_agent import SearchCard
from app.engine.config import Settings
from app.services.search_service import FEDERATION_TIMEOUT_MS, SearchService


def settings(**overrides) -> Settings:
    base = dict(environment="test", federation_enabled=True, search_cache_ttl_seconds=0)
    base.update(overrides)
    return Settings(_env_file=None, **base)


class StubFederation:
    """Stands in for the horizon at the seam the service actually uses."""

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


def service(store, federation) -> SearchService:
    svc = SearchService(store, settings())
    svc._federation = federation
    return svc


@pytest.mark.asyncio
async def test_the_ceiling_is_800_ms():
    assert FEDERATION_TIMEOUT_MS == 800


@pytest.mark.asyncio
async def test_an_empty_index_is_filled_by_the_knowledge_panel(store):
    """Cards go to `knowledge_cards`, not into `results`.

    `results` means "pages we crawled and verified". An external card in that
    slot would be a claim the index cannot back.
    """
    # The card has to name the query: one that shares no word with it is an
    # answer to a different question and is dropped.
    federation = StubFederation(
        [SearchCard(title="KSRTC bus timings", url="https://a.in/x", snippet_short="KSRTC bus timings and routes")]
    )
    payload = await service(store, federation).search("ksrtc bus timings")

    assert federation.calls == 1
    assert payload["results"] == []
    assert payload["knowledge_cards"][0]["title"] == "KSRTC bus timings"


@pytest.mark.asyncio
async def test_a_slow_fleet_is_abandoned_not_awaited(store):
    federation = StubFederation([SearchCard(title="Late", url="https://a.in/x")], delay=5)
    payload = await service(store, federation).search("nothing-matches-this-xyzzy")

    assert payload["results"] == []
    assert payload["knowledge_cards"] == []
    assert payload["total_estimate"] == 0


@pytest.mark.asyncio
async def test_a_failing_fleet_is_an_empty_page_not_a_500(store):
    federation = StubFederation(boom=True)
    payload = await service(store, federation).search("nothing-matches-this-xyzzy")

    assert payload["results"] == []


@pytest.mark.asyncio
async def test_federation_off_makes_no_call(store):
    federation = StubFederation([SearchCard(title="X", url="https://a.in/x")])
    svc = SearchService(store, settings(federation_enabled=False))
    svc._federation = federation

    payload = await svc.search("nothing-matches-this-xyzzy")

    assert federation.calls == 0
    assert payload["results"] == []


@pytest.mark.asyncio
async def test_the_conversion_happens_once(store):
    """A card that is already a dict must not be handed to `.as_dict()` again."""
    federation = StubFederation(
        [SearchCard(title="KSRTC bus", url="https://a.in/x", snippet_short="KSRTC bus timings")]
    )
    payload = await service(store, federation).search("ksrtc bus timings")

    card = payload["knowledge_cards"][0]
    assert isinstance(card, dict)
    assert card["snippet_short"] == "KSRTC bus timings"
    assert "has_more" in card
    assert "relevance" in card
