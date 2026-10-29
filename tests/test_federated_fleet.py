"""Trie-backed suggestions and agents 41 to 50."""

from __future__ import annotations

import asyncio

import pytest

from app.agents import load_agent

FLEET = (
    "41_federated_horizon_agent",
    "42_free_knowledge_courier",
    "43_external_seed_trapper",
    "44_cross_source_sanitizer",
    "45_live_entity_extractor",
    "46_async_parallel_dispatcher",
    "47_open_gov_portal_agent",
    "48_cache_warming_ambassador",
    "49_privacy_mask_agent",
    "50_supreme_marshal_agent",
)


@pytest.mark.parametrize("name", FLEET)
def test_every_fleet_agent_loads(name):
    assert load_agent(name) is not None


# ---------------------------------------------------------------------------
# Step 1 — suggest reads the trie
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_answers_from_the_trie_without_touching_the_database():
    from app.services.search_service import SearchService

    class Store:
        called = False

        async def suggest(self, query, limit=6):
            Store.called = True
            return ["from-database"]

    service = SearchService.__new__(SearchService)
    service._store = Store()
    trie = load_agent("39_autosuggest_trie_agent").TrieAutosuggestAgent()
    trie.rebuild([("bangalore", 50), ("bank", 40)])
    service.trie = trie

    assert await service.suggest("ban") == ["ban", ["bangalore", "bank"]]
    assert Store.called is False


@pytest.mark.asyncio
async def test_suggest_falls_back_to_the_database_without_a_trie():
    from app.services.search_service import SearchService

    class Store:
        async def suggest(self, query, limit=6):
            return ["from-database"]

    service = SearchService.__new__(SearchService)
    service._store = Store()

    assert await service.suggest("ban") == ["ban", ["from-database"]]


@pytest.mark.asyncio
async def test_suggest_falls_back_when_the_trie_has_no_match():
    from app.services.search_service import SearchService

    class Store:
        async def suggest(self, query, limit=6):
            return ["from-database"]

    service = SearchService.__new__(SearchService)
    service._store = Store()
    trie = load_agent("39_autosuggest_trie_agent").TrieAutosuggestAgent()
    trie.rebuild([("bangalore", 50)])
    service.trie = trie

    assert await service.suggest("zz") == ["zz", ["from-database"]]


# ---------------------------------------------------------------------------
# 41 — federation is off unless asked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_federation_does_nothing_when_disabled():
    class Settings:
        federation_enabled = False

    agent = load_agent("41_federated_horizon_agent").FederatedHorizonAgent(settings=Settings())
    result = await agent.search("anything")

    assert result.cards == []


@pytest.mark.asyncio
async def test_federation_drops_an_answer_that_misses_the_budget():
    class Settings:
        federation_enabled = True

    class SlowKnowledge:
        async def lookup(self, query):
            await asyncio.sleep(5)
            return None

    agent = load_agent("41_federated_horizon_agent").FederatedHorizonAgent(
        knowledge=SlowKnowledge(), settings=Settings(), budget=0.05
    )
    result = await agent.search("anything")

    assert result.timed_out is True
    assert result.cards == []


# ---------------------------------------------------------------------------
# 42 — the courier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_courier_reads_a_wikipedia_summary():
    class Http:
        async def get_json(self, url, timeout=1.5):
            return {
                "title": "Bengaluru",
                "extract": "Bengaluru is the capital of Karnataka.",
                "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Bengaluru"}},
            }

    card = await load_agent("42_free_knowledge_courier").FreeKnowledgeCourierAgent(
        http=Http()
    ).wikipedia("Bengaluru")

    assert card.extract.startswith("Bengaluru is")
    assert card.source == "wikipedia"


@pytest.mark.asyncio
async def test_a_failed_fetch_yields_no_card_rather_than_an_error():
    class Http:
        async def get_json(self, url, timeout=1.5):
            raise RuntimeError("network down")

    card = await load_agent("42_free_knowledge_courier").FreeKnowledgeCourierAgent(
        http=Http()
    ).lookup("anything")

    assert not card


# ---------------------------------------------------------------------------
# 44 — external results obey our rules
# ---------------------------------------------------------------------------


def test_an_external_card_is_forced_into_our_shape():
    agent = load_agent("44_cross_source_sanitizer").CrossSourceSanitizerAgent()
    card = agent.card(
        title="District Health Notifications | Some Site | Official Website",
        url="https://karnataka.gov.in/x?utm_source=ddg&ref=abc",
        snippet="y" * 300,
    )

    assert card.url == "https://karnataka.gov.in/x"
    # The ceiling the index uses, not a second number of its own.
    assert len(card.snippet) <= 200
    assert "|" not in card.title
    assert card.brand == "Karnataka"


# ---------------------------------------------------------------------------
# 45 — entities
# ---------------------------------------------------------------------------


def test_contact_details_are_sniffed_from_a_snippet():
    agent = load_agent("45_live_entity_extractor").LiveEntityExtractorAgent()
    entities = agent.extract("Call 080-22221234, PIN 560001, dated 12 October 2026.")

    assert entities.phones == ["080-22221234"]
    assert entities.pin_codes == ["560001"]
    assert entities.dates == ["12 October 2026"]


def test_a_mobile_number_is_found_too():
    entities = load_agent("45_live_entity_extractor").LiveEntityExtractorAgent().extract(
        "Mobile +91 98765 43210"
    )

    assert entities.phones == ["+91 98765 43210"]


# ---------------------------------------------------------------------------
# 46 — the dispatcher cancels what it does not need
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fast_local_answer_cancels_the_external_call():
    dispatcher = load_agent("46_async_parallel_dispatcher").AsyncParallelDispatcherAgent()
    cancelled = []

    async def local():
        # One yield, so the external task has actually started before it is
        # cancelled. Cancelling a task that never began runs no handler.
        await asyncio.sleep(0)
        return ["local"]

    async def external():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return ["external"]

    result = await dispatcher.run(local(), external())

    assert result.local == ["local"]
    assert result.cancelled_external is True
    assert cancelled == [True]


@pytest.mark.asyncio
async def test_without_federation_only_the_local_call_runs():
    dispatcher = load_agent("46_async_parallel_dispatcher").AsyncParallelDispatcherAgent()

    async def local():
        return ["local"]

    async def external():
        raise AssertionError("should not be awaited")

    result = await dispatcher.run(local(), external(), federate=False)

    assert result.local == ["local"]
    assert result.external is None


# ---------------------------------------------------------------------------
# 48 — the ambassador namespaces its cache
# ---------------------------------------------------------------------------


def test_a_federated_answer_is_cached_under_its_own_key():
    agent = load_agent("48_cache_warming_ambassador").CacheWarmingAmbassadorAgent()

    agent.set("Bengaluru", {"cards": []}, source="wikipedia")

    assert agent.get("bengaluru", source="wikipedia") == {"cards": []}
    # It must not be reachable as an indexed result.
    assert agent.get("bengaluru", source="") is None


# ---------------------------------------------------------------------------
# 49 — privacy
# ---------------------------------------------------------------------------


def test_identifying_headers_are_stripped_from_an_outbound_request():
    agent = load_agent("49_privacy_mask_agent").PrivacyMaskAgent()
    masked = agent.mask(
        {
            "Cookie": "session=abc",
            "X-Forwarded-For": "1.2.3.4",
            "Referer": "https://eyesearch.example",
            "Accept-Language": "en",
        }
    )

    assert "Cookie" not in masked
    assert "X-Forwarded-For" not in masked
    assert "Referer" not in masked
    assert masked["User-Agent"].startswith("EyesearchBot")
    assert masked["Accept-Language"] == "en"


# ---------------------------------------------------------------------------
# 50 — the marshal
# ---------------------------------------------------------------------------


def test_outbound_work_freezes_above_the_ceiling():
    agent = load_agent("50_supreme_marshal_agent").SupremeMarshalAgent(
        ceiling_bytes=800 << 20, resume_bytes=640 << 20, reader=lambda: 900 << 20
    )

    state = agent.evaluate()
    assert state.frozen is True
    assert agent.allows_outbound() is False


def test_the_marshal_does_not_flap_at_the_boundary():
    """Hysteresis: it resumes only after dropping well below the ceiling."""
    # 620 MB is below the 640 MB resume threshold; 650 would still be frozen.
    readings = iter([900 << 20, 750 << 20, 620 << 20])
    agent = load_agent("50_supreme_marshal_agent").SupremeMarshalAgent(
        ceiling_bytes=800 << 20, resume_bytes=640 << 20, reader=lambda: next(readings)
    )

    assert agent.evaluate().frozen is True
    assert agent.evaluate().frozen is True   # 750 MB: still frozen
    assert agent.evaluate().frozen is False  # 620 MB: resumed
