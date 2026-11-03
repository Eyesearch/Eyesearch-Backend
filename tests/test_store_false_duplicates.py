"""Accepted pages must reach `pages`; only a real URL collision is a duplicate."""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.engine.store import PageDocument

quality = load_agent("04_quality_agent")

#: The opening 200 characters of many Indian government pages, after the body
#: ceiling truncates them. Two unrelated sites can share this exactly.
TEMPLATE = (
    "Government of Karnataka. Skip to main content. Home About Us Contact "
    "Departments Notifications Tenders Circulars."
)


def document(url: str, body: str, digest: str | None = None) -> PageDocument:
    return PageDocument(
        url=url,
        domain=url.split("/")[2],
        title="A page",
        description="d",
        body_text=body,
        digest=digest if digest is not None else quality.page_digest(url, body),
        brand_name="A",
    )


def test_the_digest_of_a_page_includes_its_url():
    """Otherwise identical prose at two addresses produces one fingerprint."""
    left = quality.page_digest("https://a.in/x", TEMPLATE)
    right = quality.page_digest("https://b.in/y", TEMPLATE)

    assert left != right


def test_the_same_page_has_a_stable_digest():
    assert quality.page_digest("https://a.in/x", TEMPLATE) == quality.page_digest(
        "https://a.in/x", TEMPLATE
    )


@pytest.mark.asyncio
async def test_two_urls_sharing_a_body_are_both_written(client, store):
    """The production symptom: 58 accepted, 0 written, all called duplicates."""
    await store.persist_batch(
        [
            document("https://civilaviation.gov.in/dashboard", TEMPLATE),
            document("https://rmgodbole.in/lilavatis_daughters_marathi/", TEMPLATE),
        ]
    )

    pages = await client.execute("SELECT COUNT(*) FROM pages")
    assert int(pages.scalar(0) or 0) == 2


@pytest.mark.asyncio
async def test_a_page_is_reported_as_written(client, store):
    outcome = await store.persist_batch([document("https://a.gov.in/one", TEMPLATE)])

    assert outcome.pages_written == 1
    assert outcome.duplicates == 0


@pytest.mark.asyncio
async def test_the_same_url_twice_is_a_true_duplicate(client, store):
    await store.persist_batch([document("https://a.gov.in/one", TEMPLATE)])

    outcome = await store.persist_batch([document("https://a.gov.in/one", TEMPLATE)])

    assert outcome.pages_written == 0
    assert outcome.duplicates == 1


@pytest.mark.asyncio
async def test_a_batch_of_shared_bodies_writes_them_all(client, store):
    documents = [document(f"https://site{i}.gov.in/page", TEMPLATE) for i in range(10)]

    outcome = await store.persist_batch(documents)

    assert outcome.pages_written == 10
    assert outcome.duplicates == 0
    pages = await client.execute("SELECT COUNT(*) FROM pages")
    assert int(pages.scalar(0) or 0) == 10


def test_the_quality_gate_does_not_reject_a_second_page_with_the_same_body():
    """Within one batch, the same prose at another URL is another page.

    This is the same over-eager deduplication as the store bug, one layer up: a
    shared template preamble made the second page look like a repeat of the
    first, and it never reached the insert at all.
    """
    agent = quality.QualityAgent()

    first = agent.check(document("https://a.in/x", TEMPLATE))
    second = agent.check(document("https://b.in/y", TEMPLATE))

    assert first.ok is True
    assert second.ok is True


def test_the_quality_gate_still_rejects_the_same_page_twice():
    agent = quality.QualityAgent()
    agent.check(document("https://a.in/x", TEMPLATE))

    again = agent.check(document("https://a.in/x", TEMPLATE))

    assert again.ok is False
    assert again.reason == "duplicate"
