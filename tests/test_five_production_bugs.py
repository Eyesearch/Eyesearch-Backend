"""The five reported production bugs, as regression tests."""

from __future__ import annotations

import pytest

from app.engine.text import truncate_at_word


# ---------------------------------------------------------------------------
# 1. Truncation cutting mid-word
# ---------------------------------------------------------------------------


def test_a_title_is_cut_between_words_not_inside_one():
    """"Mobile Recharge Offers" at twelve characters gave "Mobile Rechar" —
    a different word, not an abbreviation of one."""
    assert truncate_at_word("Mobile Recharge Offers", 12) == "Mobile"


def test_the_cut_never_ends_in_punctuation():
    assert not truncate_at_word("Pay bills, recharge, and more", 10).endswith(",")


def test_a_short_title_is_untouched():
    assert truncate_at_word("Paytm", 50) == "Paytm"


def test_one_long_word_keeps_the_window():
    """There is no boundary to cut at; something is better than nothing."""
    assert truncate_at_word("Supercalifragilistic", 10) == "Supercalif"


def test_whitespace_is_collapsed():
    assert truncate_at_word("a   b\n c", 50) == "a b c"


def test_the_parser_uses_it_for_titles():
    import inspect

    from app.agents import load_agent

    parser = load_agent("03_parser_agent")
    source = inspect.getsource(parser)

    assert "truncate_at_word(title" in source
    assert "title[:TITLE_CHAR_CAP]" not in source


# ---------------------------------------------------------------------------
# 2. Navigational score inversion
# ---------------------------------------------------------------------------


def test_the_apex_label_is_read_past_www():
    """`split_part(domain,'.',1)` returned "www" for www.phonepe.com and "ir"
    for ir.phonepe.com, so neither matched a query of "phonepe"."""
    import inspect

    from app.engine.store import IndexStore

    source = inspect.getsource(IndexStore)
    assert "regexp_replace" in source
    assert "'^www" in source


def test_a_subdomain_does_not_collect_the_root_pin():
    import inspect

    from app.engine.store import IndexStore

    source = inspect.getsource(IndexStore)
    # The apex test: two labels after stripping www, or a known second-level
    # Indian suffix.
    assert "array_length(string_to_array(" in source
    assert "(co|gov|ac|org|net|nic|res|edu|firm|gen|ind)" in source


@pytest.mark.asyncio
async def test_the_homepage_outranks_its_own_subdomain(client, store):
    from app.engine.store import PageDocument

    body = (
        "PhonePe is an Indian digital payments and financial services company "
        "offering mobile recharge and UPI payments to its customers. "
    ) * 3
    documents = [
        PageDocument(
            url="https://www.phonepe.com/", domain="www.phonepe.com", title="PhonePe",
            description="d", body_text=body, digest="apex-1", brand_name="PhonePe",
            category="business_finance",
        ),
        PageDocument(
            url="https://ir.phonepe.com/", domain="ir.phonepe.com", title="PhonePe Investor Relations",
            description="d", body_text=body, digest="apex-2", brand_name="PhonePe",
            category="business_finance",
        ),
    ]
    await store.persist_batch(documents)

    hits = await store.search("phonepe", limit=10)

    assert hits[0].url == "https://www.phonepe.com/"
    apex = hits[0]
    subdomain = next(h for h in hits if "ir.phonepe.com" in h.url)
    assert apex.root_hit == 1
    assert subdomain.root_hit == 0


# ---------------------------------------------------------------------------
# 3. Celebrity cards
# ---------------------------------------------------------------------------


def test_a_card_that_never_names_the_query_is_dropped():
    from app.agents.federated.knowledge_blender import KnowledgeBlender

    assert KnowledgeBlender._names_the_query(
        "phonepe", {"title": "PhonePe", "snippet": "an Indian payments company"}
    )
    assert not KnowledgeBlender._names_the_query(
        "phonepe", {"title": "Dulquer Salmaan", "snippet": "an actor who endorsed an app"}
    )


def test_a_question_word_alone_does_not_qualify_a_card():
    from app.agents.federated.knowledge_blender import KnowledgeBlender

    assert not KnowledgeBlender._names_the_query(
        "what is phonepe", {"title": "Karnataka", "snippet": "a state"}
    )


def test_the_ceiling_is_small_enough_to_be_a_footnote():
    from app.agents.federated.knowledge_blender import DEFAULT_CARD_CEILING

    assert DEFAULT_CARD_CEILING <= 5


def test_there_is_a_relevance_floor():
    from app.agents.federated.knowledge_blender import MIN_CARD_RELEVANCE

    assert MIN_CARD_RELEVANCE > 0


# ---------------------------------------------------------------------------
# 4. Empty string in blend.sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_card_without_a_source_does_not_produce_an_empty_name():
    import asyncio

    from app.agents.federated.knowledge_blender import KnowledgeBlender

    card = {"title": "KSRTC bus", "url": "https://a.in/x", "snippet": "KSRTC bus timings", "source": ""}
    out = await KnowledgeBlender().blend(
        "ksrtc bus timings", [], lambda: asyncio.sleep(0, result=[card])
    )

    assert "" not in out["meta"]["sources"]
    assert out["meta"]["sources"] == ["eyesearch_index"]


@pytest.mark.asyncio
async def test_sources_are_trimmed():
    import asyncio

    from app.agents.federated.knowledge_blender import KnowledgeBlender

    card = {"title": "KSRTC bus", "url": "https://a.in/x", "snippet": "KSRTC bus timings", "source": " wikipedia "}
    out = await KnowledgeBlender().blend(
        "ksrtc bus timings", [], lambda: asyncio.sleep(0, result=[card])
    )

    assert out["meta"]["sources"] == ["wikipedia"]


# ---------------------------------------------------------------------------
# 5. Ingestion stalling
# ---------------------------------------------------------------------------


def test_the_crawler_presents_a_real_browser():
    """Bot protection is answered with a browser identity, not a bare name."""
    from app.engine.config import DESKTOP_USER_AGENT

    assert "Mozilla/5.0" in DESKTOP_USER_AGENT
    assert "Chrome/" in DESKTOP_USER_AGENT


def test_the_fetch_carries_the_headers_a_browser_sends():
    from app.agents import load_agent

    headers = load_agent("02_fetcher_agent").BOUND_HEADERS

    for name in ("Accept", "Accept-Language", "Sec-Fetch-Mode", "Upgrade-Insecure-Requests"):
        assert name in headers, name


@pytest.mark.asyncio
async def test_a_failed_fetch_leaves_pending(client, store):
    """A URL that fails moves to `failed`, it does not sit pending for ever."""
    await store.enqueue_urls([("https://dead.in/x", "dead.in")])
    queued = await store.pending_batch(limit=1)

    await store.mark_failed([queued[0].rowid], reason="timeout")

    row = await client.execute(
        "SELECT status, retry_count FROM crawl_queue WHERE id = ?", (queued[0].rowid,)
    )
    assert row.rows[0][0] == "failed"
    assert int(row.rows[0][1] or 0) == 1
