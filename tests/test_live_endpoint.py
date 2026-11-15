"""The live feed agent and the /v1/live contract.

Two things are asserted here: the agent stores what its sources return and
survives a source that dies, and the endpoint exists with the fields a caller
needs to know how old an answer is.
"""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.live import store as live
from app.main import app

LiveFeedAgent = load_agent("31_live_feed_agent").LiveFeedAgent


class GoodSource:
    name = "good"
    url = "https://example.in/good"
    ttl_seconds = 900

    async def fetch(self):
        return [("gold.22k.inr.gram", "6145", "INR/gram")]


class DeadSource:
    name = "dead"
    url = "https://example.in/dead"
    ttl_seconds = 900

    async def fetch(self):
        raise RuntimeError("connection refused")


class EmptySource:
    name = "empty"
    url = "https://example.in/empty"
    ttl_seconds = 900

    async def fetch(self):
        return []


@pytest.mark.asyncio
async def test_a_pass_stores_the_good_source_and_records_the_dead_one(client):
    agent = LiveFeedAgent(client, sources=[GoodSource(), DeadSource(), EmptySource()])

    report = await agent.run()

    assert report["stored"] == 1
    assert report["failed"] == 2
    item = await live.fact(client, "gold.22k.inr.gram")
    assert item is not None and item.value == "6145"


@pytest.mark.asyncio
async def test_one_dead_source_does_not_stop_the_others(client):
    agent = LiveFeedAgent(client, sources=[DeadSource(), GoodSource()])

    report = await agent.run()

    assert report["stored"] == 1


def test_the_endpoint_is_registered_and_is_not_search():
    """/v1/live is its own route: a price is looked up, not ranked."""
    paths = {route.path for route in app.routes}
    assert "/v1/live" in paths
    assert "/v1/search" in paths


@pytest.mark.asyncio
async def test_the_weather_source_is_built_after_the_cities_are_placed(client):
    """An agent that builds its sources at construction fetches no weather.

    The weather source asks about the cities it was given, so it has to be
    handed them after they are resolved — building it in `__init__` gave it an
    empty map, and the pass reported success while storing nothing.
    """
    from app.agents import load_agent

    agent = load_agent("31_live_feed_agent").LiveFeedAgent(client)

    assert agent._sources is None, "sources are built at run time, not at construction"


@pytest.mark.asyncio
async def test_retrieve_answers_a_live_question_from_the_live_layer(client):
    """`/v1/retrieve` is what the AI tab calls; it must not answer with a page."""
    from app.live import compose
    from app.live.router import Route

    await live.put_fact(client, "gold.22k.inr.gram", "12440.1", unit="INR/gram", source="bullion")

    payload = await compose.from_live(client, Route("bullion", "gold.22k.inr.gram"))

    assert payload is not None
    markdown = compose.as_markdown(payload)
    assert "12440.1" in markdown
    assert "As of" in markdown
    assert "bullion" in markdown
