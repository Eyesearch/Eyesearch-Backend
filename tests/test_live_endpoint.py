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
