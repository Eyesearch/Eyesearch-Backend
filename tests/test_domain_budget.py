"""The strict per-page body ceiling and the domain total.

Every page is stored with at most `page_body_char_ceiling` characters of body
text — 220 by default, about two lines of card text. The cap is flat: a domain
with one page gets the same allowance as one with twenty-five. The domain total
has to clear the ceiling times the page cap, or it silently undoes the ceiling.
"""

from __future__ import annotations

import pytest

from app.engine.config import Settings
from app.engine.store import IndexStore, PageDocument

BODY = "The department publishes district health notifications for every office. " * 20


def settings(ceiling: int = 200, budget: int = 5000) -> Settings:
    return Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
        page_body_char_ceiling=ceiling,
        domain_body_budget_chars=budget,
    )


def page(url: str, domain: str, digest: str) -> PageDocument:
    return PageDocument(
        url=url,
        domain=domain,
        title="District health notices",
        description="d",
        body_text=BODY,
        digest=digest,
    )


async def body_lengths(client) -> list[int]:
    result = await client.execute("SELECT length(body_text) FROM pages ORDER BY page_id")
    return [int(row[0]) for row in result.rows]


@pytest.mark.asyncio
async def test_every_page_is_capped_at_the_ceiling(client):
    store = IndexStore(client, settings())
    await store.persist_batch([page("https://a.gov.in/1", "a.gov.in", "b1")])

    assert len(BODY) > 200, "the fixture must be longer than the ceiling"
    lengths = await body_lengths(client)
    # At most the ceiling: the cut lands on a word boundary, so it can be shorter.
    assert lengths[0] <= 200
    assert lengths[0] > 100


@pytest.mark.asyncio
async def test_the_cap_does_not_shrink_as_a_domain_grows(client):
    """Flat, not divided: page four keeps the same 40 as page one."""
    store = IndexStore(client, settings())
    await store.persist_batch([page("https://a.gov.in/1", "a.gov.in", "b1")])
    first = (await body_lengths(client))[0]
    assert first <= 200

    await store.persist_batch(
        [page(f"https://a.gov.in/{i}", "a.gov.in", f"b{i}") for i in (2, 3, 4)]
    )
    # Flat: the fourth page keeps the same allowance as the first.
    assert await body_lengths(client) == [first, first, first, first]


@pytest.mark.asyncio
async def test_a_domain_of_twenty_five_pages_stays_within_the_total(client):
    """40 x 25 is exactly the 1,000-character domain limit."""
    store = IndexStore(client, settings())
    await store.persist_batch(
        [page(f"https://a.gov.in/{i}", "a.gov.in", f"b{i}") for i in range(25)]
    )

    lengths = await body_lengths(client)
    assert len(lengths) == 25
    assert all(length <= 200 for length in lengths)
    assert sum(lengths) <= 5000


@pytest.mark.asyncio
async def test_a_second_domain_is_capped_the_same_way(client):
    store = IndexStore(client, settings())
    await store.persist_batch(
        [
            page("https://a.gov.in/1", "a.gov.in", "b1"),
            page("https://b.example/1", "b.example", "b2"),
        ]
    )

    lengths = await body_lengths(client)
    assert lengths[0] == lengths[1] <= 200


@pytest.mark.asyncio
async def test_a_shorter_body_is_left_alone(client):
    store = IndexStore(client, settings())
    short = PageDocument(
        url="https://a.gov.in/short",
        domain="a.gov.in",
        title="Short",
        description="d",
        body_text="A short note.",
        digest="b9",
    )
    await store.persist_batch([short])

    assert await body_lengths(client) == [len("A short note.")]


@pytest.mark.asyncio
async def test_a_ceiling_of_zero_disables_the_cap(client):
    store = IndexStore(client, settings(ceiling=0, budget=0))
    await store.persist_batch([page("https://a.gov.in/1", "a.gov.in", "b1")])

    assert await body_lengths(client) == [len(BODY)]


def test_the_production_defaults_leave_room_for_the_ceiling():
    """The tests above set the ceiling explicitly; this pins the real defaults."""
    from app.engine.config import Settings as RealSettings

    default = RealSettings(_env_file=None)
    assert default.page_body_char_ceiling == 200
    # The domain total must clear ceiling x page cap, or it clamps every page
    # back down and the ceiling above is a lie.
    assert default.domain_body_budget_chars >= (
        default.page_body_char_ceiling * default.max_pages_per_domain
    )


@pytest.mark.asyncio
async def test_a_stored_body_never_ends_mid_word(client):
    """`substr(text, 1, 40)` cut "…southern India, ne" and the card showed the
    "ne". A stored body must end on a whole word."""
    store = IndexStore(client, settings())
    body = ("Karnataka is in southern India, near the Western Ghats and the Deccan plateau, "
            "and its capital is Bengaluru, which is also the state's largest city by population. ") * 3
    await store.persist_batch(
        [
            PageDocument(
                url="https://a.gov.in/1",
                domain="a.gov.in",
                title="T",
                description="d",
                body_text=body,
                digest="w1",
            )
        ]
    )

    stored = await client.execute("SELECT body_text FROM pages")
    text = stored.scalar(0)

    assert len(text) <= 200
    assert text.split()[-1] in body.split(), f"ended mid-word: {text!r}"
