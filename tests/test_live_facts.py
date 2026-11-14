"""Live facts: stored, read back, expired, and never confused with the index."""

from __future__ import annotations

import pytest

from app.engine.config import Settings
from app.engine.store import IndexStore
from app.live import store as live
from app.live.answer import fact_answer, facts_answer, missing_answer
from app.live.registry import record_fail, record_ok, source_health


@pytest.mark.asyncio
async def test_a_fact_is_stored_and_read_back_with_its_age(client):
    await live.put_fact(
        client,
        "weather.bengaluru.temperature",
        "27.4",
        unit="°C",
        source="weather",
        ttl_seconds=900,
    )

    item = await live.fact(client, "weather.bengaluru.temperature")

    assert item is not None
    assert item.value == "27.4"
    assert item.unit == "°C"
    assert item.source == "weather"
    assert item.fresh is True

    answer = fact_answer(item)
    assert answer["answer"] == "27.4 °C"
    assert answer["freshness"] == "live"
    assert answer["as_of"]


@pytest.mark.asyncio
async def test_a_fact_past_its_ttl_is_stale_not_missing(client):
    """Stale and missing are different answers, and the caller must see which.

    A price an hour old is worth reporting with its time on it. A price we never
    had is worth nothing, and saying "unknown" is the honest answer.
    """
    await live.put_fact(client, "gold.22k.inr.gram", "6145", unit="INR/gram", ttl_seconds=0)

    item = await live.fact(client, "gold.22k.inr.gram")

    assert item is not None
    assert item.fresh is False
    assert fact_answer(item)["freshness"] == "stale"


@pytest.mark.asyncio
async def test_a_missing_fact_says_so(client):
    assert await live.fact(client, "nothing.here") is None
    assert missing_answer("nothing.here")["freshness"] == "missing"


@pytest.mark.asyncio
async def test_expiry_deletes_only_what_has_passed(client):
    await live.put_fact(client, "fresh.one", "1", ttl_seconds=900)
    await live.put_fact(client, "old.one", "2", ttl_seconds=0)

    removed = await live.expire(client)

    assert removed >= 1
    assert await live.fact(client, "fresh.one") is not None
    assert await live.fact(client, "old.one") is None


@pytest.mark.asyncio
async def test_listing_shows_what_a_source_produced(client):
    await live.put_fact(client, "news.pib.1", "Headline", unit="https://pib.gov.in/x")
    await live.put_fact(client, "news.pib.2", "Second", unit="https://pib.gov.in/y")
    await live.put_fact(client, "weather.delhi.temperature", "31", unit="°C")

    news = await live.all_facts(client, prefix="news.")
    payload = facts_answer(news)

    assert payload["count"] == 2
    assert {entry["key"] for entry in payload["facts"]} == {"news.pib.1", "news.pib.2"}


@pytest.mark.asyncio
async def test_source_health_records_failures_until_a_success_clears_them(client):
    await record_fail(client, "bullion", "timeout")
    await record_fail(client, "bullion", "timeout")
    health = {entry.name: entry for entry in await source_health(client)}
    assert health["bullion"].fail_count == 2

    await record_ok(client, "bullion")
    health = {entry.name: entry for entry in await source_health(client)}
    assert health["bullion"].fail_count == 0
    assert health["bullion"].healthy is True


@pytest.mark.asyncio
async def test_the_live_tables_are_not_the_pages_table(client):
    """The separation, asserted rather than assumed.

    A live value in `pages` would be ranked, aged by the crawler's rules and
    counted against the index ceiling — which is exactly the mixing this layer
    exists to avoid.
    """
    store = IndexStore(client, Settings(environment="test"))
    await live.put_fact(client, "gold.24k.inr.gram", "6500", unit="INR/gram")

    result = await client.execute("SELECT COUNT(*) FROM pages")
    assert int(result.scalar(0) or 0) == 0
    assert store is not None
