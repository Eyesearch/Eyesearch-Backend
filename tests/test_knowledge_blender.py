"""The blender: ranking, the budget, and the organic result's immunity."""

from __future__ import annotations

import asyncio

import pytest

from app.agents.federated.knowledge_blender import (
    DEFAULT_TIMEOUT_MS,
    HERO_THRESHOLD,
    KnowledgeBlender,
)
from app.agents.federated.knowledge_courier import KnowledgeCard


def card(title, url, text, source="wikipedia"):
    return KnowledgeCard(title=title, url=url, extract=text, source=source)


ORGANIC = [
    {"title": "Karnataka Bank", "url": "https://karnatakabank.com/personal", "snippet": "Banking"},
    {"title": "Government of Karnataka", "url": "https://karnataka.gov.in/", "snippet": "State portal"},
]


def blender(**kwargs) -> KnowledgeBlender:
    return KnowledgeBlender(**kwargs)


async def cards_now(items):
    return items


# ---------------------------------------------------------------------------
# The fast path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_organic_and_cards_are_both_returned():
    out = await blender().blend(
        "karnataka history",
        ORGANIC,
        lambda: cards_now([card("Karnataka", "https://en.wikipedia.org/wiki/Karnataka", "Karnataka is a state with a long history.")]),
    )

    assert len(out["organic_results"]) == 2
    assert len(out["knowledge_cards"]) == 1
    assert out["meta"]["degraded"] is False


@pytest.mark.asyncio
async def test_the_payload_has_every_contract_field():
    out = await blender().blend("q", ORGANIC, lambda: cards_now([]))

    assert set(out) >= {"query", "organic_results", "knowledge_cards", "primary_card", "meta"}
    assert set(out["meta"]) >= {
        "intent", "organic_count", "knowledge_count", "sources", "took_ms", "degraded"
    }


@pytest.mark.asyncio
async def test_an_empty_courier_answer_is_not_an_error():
    out = await blender().blend("q", ORGANIC, lambda: cards_now([]))

    assert out["knowledge_cards"] == []
    assert out["primary_card"] is None
    assert len(out["organic_results"]) == 2
    assert out["meta"]["degraded"] is False


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------


def test_the_default_budget_is_850_ms():
    assert DEFAULT_TIMEOUT_MS == 850


@pytest.mark.asyncio
async def test_a_slow_fleet_loses_its_cards_but_not_the_results():
    async def slow():
        await asyncio.sleep(5)
        return [card("Late", "https://a.in/x", "text")]

    out = await blender(timeout_ms=50).blend("q", ORGANIC, slow)

    assert out["knowledge_cards"] == []
    assert out["organic_results"] == ORGANIC
    assert out["meta"]["degraded"] is True


@pytest.mark.asyncio
async def test_a_failing_fleet_never_costs_a_search_result():
    async def boom():
        raise RuntimeError("provider exploded")

    out = await blender().blend("q", ORGANIC, boom)

    assert out["organic_results"] == ORGANIC
    assert out["meta"]["degraded"] is True


@pytest.mark.asyncio
async def test_a_timed_out_task_is_cancelled():
    cancelled = {"yes": False}

    async def hangs():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled["yes"] = True
            raise
        return []

    await blender(timeout_ms=30).blend("q", ORGANIC, hangs)

    assert cancelled["yes"] is True


@pytest.mark.asyncio
async def test_no_federated_leg_at_all_still_answers():
    out = await blender().blend("q", ORGANIC, None)

    assert out["organic_results"] == ORGANIC
    assert out["meta"]["degraded"] is False


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_card_pointing_at_a_crawled_page_is_dropped():
    cards = [card("Karnataka Bank", "https://karnatakabank.com/personal", "the same page")]

    out = await blender().blend("karnataka bank", ORGANIC, lambda: cards_now(cards))

    assert out["knowledge_cards"] == []


@pytest.mark.asyncio
async def test_cards_are_ordered_by_relevance():
    cards = [
        card("Unrelated article", "https://a.in/x", "nothing to do with the query at all"),
        card("Karnataka history", "https://b.in/y", "Karnataka history from the empires onward"),
    ]

    out = await blender().blend("karnataka history", [], lambda: cards_now(cards))

    assert out["knowledge_cards"][0]["title"] == "Karnataka history"
    assert out["knowledge_cards"][0]["relevance"] > out["knowledge_cards"][1]["relevance"]


@pytest.mark.asyncio
async def test_a_duplicate_card_url_appears_once():
    cards = [
        card("Karnataka", "https://a.in/x", "text one"),
        card("Karnataka", "https://a.in/x#frag", "text two"),
    ]

    out = await blender().blend("karnataka", [], lambda: cards_now(cards))

    assert len(out["knowledge_cards"]) == 1


def test_the_ceiling_is_respected():
    cards = [card(f"Article number {i}", f"https://a{i}.in/x", "body") for i in range(30)]

    ranked = blender(ceiling=5).rank("article", cards)

    assert len(ranked) == 5


def test_question_words_do_not_dilute_the_score():
    """`what is karnataka` must score the Karnataka article on its one real word."""
    scored = blender().score("what is karnataka", card("Karnataka", "https://a.in/x", "Karnataka is a state."))

    assert scored.score >= HERO_THRESHOLD


# ---------------------------------------------------------------------------
# Hero card and intent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_confident_card_becomes_the_hero():
    cards = [card("Karnataka", "https://en.wikipedia.org/wiki/Karnataka", "Karnataka is a state in south western India with Bengaluru as its capital city.")]

    out = await blender().blend("what is karnataka", [], lambda: cards_now(cards))

    assert out["primary_card"] is not None
    assert out["primary_card"]["title"] == "Karnataka"


@pytest.mark.asyncio
async def test_a_weak_card_is_not_a_hero_but_is_still_listed():
    cards = [card("Something", "https://a.in/x", "barely related")]

    out = await blender().blend("karnataka history", [], lambda: cards_now(cards))

    assert out["primary_card"] is None
    assert len(out["knowledge_cards"]) == 1


def test_an_informational_query_is_recognised():
    assert blender().is_informational("what is karnataka") is True
    assert blender().is_informational("who is kalidasa") is True
    assert blender().is_informational("karnataka bank login") is False


def test_a_navigational_query_is_recognised():
    assert blender().is_navigational("karnataka bank login", ORGANIC) is True
    assert blender().is_navigational("what is photosynthesis", ORGANIC) is False


@pytest.mark.asyncio
async def test_a_navigational_query_does_not_get_a_hero_card():
    """The reader typed a destination, not a question."""
    cards = [card("Karnataka Bank", "https://en.wikipedia.org/wiki/Karnataka_Bank", "Karnataka Bank is an Indian bank founded in 1924.")]

    out = await blender().blend("karnataka bank login", ORGANIC, lambda: cards_now(cards))

    assert out["meta"]["intent"] == "navigational"
    assert out["primary_card"] is None


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_the_blender_never_touches_the_database():
    import app.agents.federated.knowledge_blender as module

    source = open(module.__file__, encoding="utf-8").read()
    for forbidden in ("app.engine.store", "app.postgres", "crawl_queue", "INSERT INTO"):
        assert forbidden not in source
