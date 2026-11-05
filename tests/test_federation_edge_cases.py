"""Six production edge cases in the federated fallback."""

from __future__ import annotations

import asyncio
import re

import pytest

from app.agents.federated import knowledge_courier as courier
from app.agents.federated.horizon_agent import FederatedHorizonAgent, SNIPPET_SHORT_CHARS
from app.engine.config import Settings
from app.services.search_service import FEDERATION_TIMEOUT_MS, SearchService


def settings(**overrides) -> Settings:
    base = dict(environment="test", federation_enabled=True)
    base.update(overrides)
    return Settings(_env_file=None, **base)


# ---------------------------------------------------------------------------
# 1 — a redirect must not become a bypass
# ---------------------------------------------------------------------------


def test_the_client_does_not_follow_redirects():
    """Following one would skip the guard entirely: it only sees the first URL."""
    source = open(courier.__file__, encoding="utf-8").read()

    assert "follow_redirects=False" in source
    assert "follow_redirects=True" not in source


# ---------------------------------------------------------------------------
# 2 — a hung resolver must not eat the request budget
# ---------------------------------------------------------------------------


def test_dns_has_its_own_shorter_budget():
    """500 ms: 300 was tight enough to fail cold lookups on a cloud resolver,
    and it still has to leave room inside the 800 ms request budget."""
    assert courier.DNS_TIMEOUT_SECONDS == 0.5
    assert courier.DNS_TIMEOUT_SECONDS < courier.TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_a_hung_resolver_returns_nothing_quickly(monkeypatch):
    import time

    def hang(url, *, resolve=True):
        time.sleep(2)
        return True, ""

    monkeypatch.setattr(courier, "is_public_url", hang)
    agent = courier.FreeKnowledgeCourierAgent()
    started = time.perf_counter()

    payload = await agent._get_json("https://example.com/x")

    assert payload == {}
    assert time.perf_counter() - started < 1.0


# ---------------------------------------------------------------------------
# 3 — the task behind a timeout is cancelled, not left running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_timed_out_fallback_task_is_cancelled(store):
    cancelled = {"yes": False}

    class Hanging:
        async def search(self, query, *, limit=10):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled["yes"] = True
                raise
            return []

    service = SearchService(store, settings())
    service._federation = Hanging()

    payload = await service.search("nothing-matches-this-xyzzy")

    assert payload["results"] == []
    assert cancelled["yes"] is True
    assert 1200 <= FEDERATION_TIMEOUT_MS <= 2000


# ---------------------------------------------------------------------------
# 4 — markup and entities must be gone before lengths are measured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_markup_is_stripped_before_the_length_is_measured():
    class Courier:
        async def search(self, query, limit=10):
            return [
                {
                    "title": "<b>Result</b>",
                    "url": "https://a.in/x",
                    "snippet": "<mark>term</mark> &quot;quoted&quot; &amp; " + "word " * 100,
                }
            ]

    result = await FederatedHorizonAgent(
        knowledge=Courier(), settings=settings()
    ).search("q")
    card = result.cards[0].as_dict()

    assert "<b>" not in card["title"]
    assert "<mark>" not in card["snippet_short"]
    assert "&quot;" not in card["snippet_short"]
    assert "&amp;" not in card["snippet_short"]
    assert len(card["snippet_short"]) <= SNIPPET_SHORT_CHARS
    assert not re.search(r"<[^>]+>", card["snippet_full"])


# ---------------------------------------------------------------------------
# 5 — the envelope must say there is no page two
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_federated_page_says_it_is_the_only_page(store):
    from app.agents.federated.horizon_agent import SearchCard

    class Fleet:
        async def search(self, query, *, limit=10):
            return [SearchCard(title="T", url="https://a.in/x", snippet_short="s").as_dict()]

    service = SearchService(store, settings())
    service._federation = Fleet()

    payload = await service.search("nothing-matches-this-xyzzy")

    assert payload["total_pages"] == 1
    assert payload["has_next_page"] is False


@pytest.mark.asyncio
async def test_an_empty_page_still_carries_the_fields(store):
    service = SearchService(store, settings(federation_enabled=False))

    payload = await service.search("nothing-matches-this-xyzzy")

    assert payload["total_pages"] == 1
    assert payload["has_next_page"] is False


# ---------------------------------------------------------------------------
# 6 — a burst of empty searches must not become a burst of requests
# ---------------------------------------------------------------------------


def test_the_concurrency_cap_is_five():
    assert courier.MAX_CONCURRENT_REQUESTS == 5


@pytest.mark.asyncio
async def test_the_gate_bounds_simultaneous_requests(monkeypatch):
    """Six concurrent fetches must never exceed five inside the gate."""
    active = {"now": 0, "peak": 0}

    class Agent(courier.FreeKnowledgeCourierAgent):
        async def _fetch_json(self, client, url):
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            try:
                await asyncio.sleep(0.05)
                return {}
            finally:
                active["now"] -= 1

    agent = Agent()
    monkeypatch.setattr(agent, "_ensure_client", lambda: object())

    await asyncio.gather(*[agent._get_json(f"https://a.in/{i}") for i in range(12)])

    assert active["peak"] <= courier.MAX_CONCURRENT_REQUESTS
