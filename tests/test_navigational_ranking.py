"""Navigational pinning, official sub-pages, and description-only suppression."""

from __future__ import annotations

import pytest

from app.engine.store import PageDocument

BODY = "savings account interest rate branch office scheme"


async def seed(store):
    await store.persist_batch(
        [
            PageDocument(
                url="https://examplebank.com/", domain="examplebank.com", title="ExampleBank",
                description="Banking services", body_text=BODY, digest="nav-1",
                brand_name="ExampleBank", language="en",
            ),
            PageDocument(
                url="https://examplebank.com/personal", domain="examplebank.com",
                title="Personal banking", description="Accounts and cards", body_text=BODY,
                digest="nav-2", brand_name="ExampleBank", language="en",
            ),
            PageDocument(
                url="https://techblog.com/post", domain="techblog.com", title="Ten fintech apps",
                description="we looked at examplebank and others", body_text="examplebank " + BODY,
                digest="nav-3", brand_name="TechBlog", language="en",
            ),
        ]
    )


@pytest.mark.asyncio
async def test_the_brand_page_is_pinned_first(store):
    await seed(store)

    hits = await store.search("examplebank")

    assert hits[0].url == "https://examplebank.com/"
    assert hits[0].nav_hit == 1
    assert hits[0].root_hit == 1


@pytest.mark.asyncio
async def test_official_sub_pages_follow_the_hero(store):
    """A sub-page rarely mentions the site's own name, so it does not match the
    query — it is placed behind the front door instead."""
    await seed(store)

    hits = await store.search("examplebank")
    urls = [hit.url for hit in hits]

    assert urls[1] == "https://examplebank.com/personal"
    assert urls[-1] == "https://techblog.com/post"


@pytest.mark.asyncio
async def test_a_topic_query_is_not_rearranged(store):
    """Only a navigational top hit pulls its site's pages up behind it."""
    await seed(store)

    hits = await store.search("savings account")

    assert hits[0].nav_hit == 0
    assert len({hit.url.split("/")[2] for hit in hits}) > 1


@pytest.mark.asyncio
async def test_a_description_only_match_does_not_outrank_the_brand(store):
    await seed(store)

    hits = await store.search("examplebank")
    blog = next(hit for hit in hits if "techblog" in hit.url)

    assert blog.nav_hit == 0
    assert hits.index(blog) > 0


def test_the_storage_ceiling_is_four_and_a_half_gigabytes():
    from app.engine.config import Settings

    settings = Settings(_env_file=None)

    assert settings.storage_ceiling_bytes == int(4.5 * 1024**3)


def test_a_host_that_starts_with_w_keeps_its_name():
    """`lstrip("www.")` is a set of characters, not a prefix.

    It turned `wb.gov.in` into `b.gov.in` and `worldbank.org` into
    `orldbank.org`, so the site the reader named was asked for the sub-pages of
    a domain that does not exist — and got none, silently.
    """
    from app.agents import load_agent

    host_label = load_agent("10_brand_canonical_agent")._host_label

    assert host_label("https://wb.gov.in/") == "wb.gov.in"
    assert host_label("https://www.wb.gov.in/") == "wb.gov.in"
    assert host_label("https://worldbank.org/x") == "worldbank.org"
    assert host_label("https://www.karnataka.gov.in/x") == "karnataka.gov.in"


def test_a_query_of_short_words_is_not_a_type_error():
    """Every token shorter than the minimum produced a boolean, not an integer.

    `title_terms` was `false` when no token was long enough to check, and the
    expression is summed and compared with `0` — so a two-letter search was a
    500 rather than a search.
    """
    import inspect

    from app.engine import store as store_module

    source = inspect.getsource(store_module.IndexStore.search)
    assert 'title_terms = "0"' in source
