"""The quarantine ratio, and moving stored pages out of it."""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.engine.store import PageDocument

supervisor = load_agent("14_king_orchestrator_agent")
BODY = " ".join(["service", "portal", "information"] * 10)


async def seed(store, client, documents):
    await store.persist_batch(documents)
    # Everything starts in WEB, as an index built before the taxonomy would be.
    await client.execute(
        "UPDATE pages SET category_id = (SELECT id FROM categories WHERE code = 'WEB')"
    )


@pytest.mark.asyncio
async def test_the_ratio_is_reported(client, store):
    await seed(
        store,
        client,
        [
            PageDocument(url="https://a.example/1", domain="a.example", title="One",
                         description="d", body_text=BODY, digest="q1", brand_name="A"),
            PageDocument(url="https://b.example/1", domain="b.example", title="Two",
                         description="d", body_text=BODY, digest="q2", brand_name="B"),
        ],
    )

    report = await supervisor.web_ratio(client)

    assert report["total_pages"] == 2
    assert report["web_ratio"] == 1.0
    assert report["breach"] is True


@pytest.mark.asyncio
async def test_a_small_quarantine_does_not_breach(client, store):
    documents = [
        PageDocument(url=f"https://gov{i}.gov.in/x", domain=f"gov{i}.gov.in",
                     title=f"Portal {i}", description="d", body_text=BODY,
                     digest=f"ok{i}", brand_name=f"G{i}")
        for i in range(9)
    ] + [
        PageDocument(url="https://ablog.example/x", domain="ablog.example",
                     title="Thoughts", description="d", body_text=BODY,
                     digest="ok-web", brand_name="Blog"),
    ]
    await store.persist_batch(documents)
    await client.execute(
        "UPDATE pages SET category_id = (SELECT id FROM categories WHERE code = 'GOV') "
        "WHERE url LIKE 'https://gov%.gov.in/%'"
    )

    report = await supervisor.web_ratio(client)

    assert report["web_pages"] == 1
    assert report["web_ratio"] == pytest.approx(0.1, abs=0.001)


@pytest.mark.asyncio
async def test_an_empty_index_does_not_divide_by_zero(client):
    report = await supervisor.web_ratio(client)

    assert report["total_pages"] == 0
    assert report["web_ratio"] == 0.0
    assert report["breach"] is False


def test_the_threshold_is_a_tenth():
    assert supervisor.WEB_RATIO_WARNING == 0.10
