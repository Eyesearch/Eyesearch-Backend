"""Per-domain result clustering and vocabulary suggestions."""

from __future__ import annotations

import pytest

from app.engine.store import IndexStore, PageDocument
from app.services.search_service import cluster_by_domain

BODY = "karnataka health district notification office government scheme passport " * 2


class Hit:
    def __init__(self, url: str) -> None:
        self.url = url


def urls(hits) -> list[str]:
    return [hit.url for hit in hits]


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def test_one_domain_cannot_take_the_whole_page():
    hits = [Hit(f"https://a.com/{i}") for i in range(8)]

    assert urls(cluster_by_domain(hits, per_domain=2, limit=10)) == [
        "https://a.com/0",
        "https://a.com/1",
    ]


def test_other_domains_get_through():
    hits = [Hit(f"https://a.com/{i}") for i in range(5)] + [Hit("https://b.com/1")]

    assert urls(cluster_by_domain(hits, per_domain=2, limit=3)) == [
        "https://a.com/0",
        "https://a.com/1",
        "https://b.com/1",
    ]


def test_rank_order_is_preserved():
    hits = [Hit("https://a.com/1"), Hit("https://b.com/1"), Hit("https://a.com/2")]

    assert urls(cluster_by_domain(hits, per_domain=1, limit=10)) == [
        "https://a.com/1",
        "https://b.com/1",
    ]


def test_a_cap_of_zero_means_no_cap():
    hits = [Hit(f"https://a.com/{i}") for i in range(4)]

    assert len(cluster_by_domain(hits, per_domain=0, limit=4)) == 4


@pytest.mark.asyncio
async def test_the_service_returns_a_spread_of_domains(client, settings):
    from app.services.search_service import SearchService

    store = IndexStore(client, settings)
    await store.persist_batch(
        [
            PageDocument(
                url=f"https://a.gov.in/{i}", domain="a.gov.in", title=f"Karnataka health {i}",
                description="d", body_text=BODY, digest=f"cl-a{i}", brand_name="A",
            )
            for i in range(6)
        ]
        + [
            PageDocument(
                url="https://b.gov.in/1", domain="b.gov.in", title="Karnataka health b",
                description="d", body_text=BODY, digest="cl-b1", brand_name="B",
            )
        ]
    )

    service = SearchService(store, settings)
    payload = await service.search("karnataka health", limit=5)
    hosts = [item["url"].split("/")[2] for item in payload["results"]]

    assert hosts.count("a.gov.in") <= settings.max_results_per_domain
    assert "b.gov.in" in hosts


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_suggestion_is_a_word_not_a_title(client, settings):
    store = IndexStore(client, settings)
    await store.persist_batch(
        [
            PageDocument(
                url="https://a.gov.in/1", domain="a.gov.in",
                title="District Health Notifications for Every Office",
                description="d", body_text=BODY, digest="sg-1", brand_name="A",
            )
        ]
    )

    suggestions = await store.suggest("karn")

    assert suggestions == ["karnataka"]


@pytest.mark.asyncio
async def test_a_one_character_prefix_suggests_nothing(client, settings):
    store = IndexStore(client, settings)

    assert await store.suggest("k") == []
