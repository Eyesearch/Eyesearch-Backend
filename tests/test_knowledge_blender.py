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
        card("Karnataka history overview", "https://b.in/y", "Karnataka history from the empires onward"),
        card("Karnataka history notes", "https://a.in/x", "Karnataka history in brief"),
    ]

    out = await blender().blend("karnataka history", [], lambda: cards_now(cards))

    scores = [c["relevance"] for c in out["knowledge_cards"]]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_a_card_that_never_names_the_query_is_dropped():
    """A search for a payment app returned the actors who advertise it: the
    words never appear, but the source ranked them and authority carried them
    over the line."""
    cards = [
        card("PhonePe", "https://a.in/x", "PhonePe is an Indian payments company."),
        card("Dulquer Salmaan", "https://b.in/y", "An Indian actor who endorsed the app."),
    ]

    out = await blender().blend("phonepe", [], lambda: cards_now(cards))

    assert [c["title"] for c in out["knowledge_cards"]] == ["PhonePe"]


@pytest.mark.asyncio
async def test_a_duplicate_card_url_appears_once():
    cards = [
        card("Karnataka", "https://a.in/x", "text one"),
        card("Karnataka", "https://a.in/x#frag", "text two"),
    ]

    out = await blender().blend("karnataka", [], lambda: cards_now(cards))

    assert len(out["knowledge_cards"]) == 1


def test_the_ceiling_is_respected():
    cards = [
        card(f"Article number {i} about articles", f"https://a{i}.in/x", "an article about article number")
        for i in range(30)
    ]

    ranked = blender(ceiling=5).rank("article", cards)

    assert len(ranked) == 5


def test_the_default_ceiling_is_small():
    """A row of twenty chips under an answer is clutter, not an answer."""
    from app.agents.federated.knowledge_blender import DEFAULT_CARD_CEILING

    assert DEFAULT_CARD_CEILING <= 5


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
async def test_a_card_that_only_half_matches_is_not_a_hero():
    """It mentions one of the words; that is not an answer to the question."""
    cards = [card("Karnataka", "https://a.in/x", "Karnataka")]

    out = await blender().blend("karnataka history geography culture", [], lambda: cards_now(cards))

    assert out["primary_card"] is None


@pytest.mark.asyncio
async def test_a_card_below_the_floor_is_not_shown():
    """One word out of five, in a snippet that says nothing else."""
    cards = [card("Karnataka", "https://a.in/x", "Karnataka")]

    out = await blender().blend(
        "karnataka history geography culture language", [], lambda: cards_now(cards)
    )

    assert out["knowledge_cards"] == []


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


# ---------------------------------------------------------------------------
# Exact title promotion
# ---------------------------------------------------------------------------


def test_an_exact_title_match_scores_full_marks():
    """Relevance measures how much a page talks about a subject; a title match
    is the subject itself."""
    from app.agents.federated.knowledge_blender import EXACT_MATCH_SCORE

    ranked = blender().rank("karnataka", [card("Karnataka", "https://a.in/x", "a state")])

    assert EXACT_MATCH_SCORE == 1.0
    assert ranked[0].score == EXACT_MATCH_SCORE


def test_an_exact_title_outranks_a_denser_article():
    cards = [
        card(
            "Architecture of Karnataka",
            "https://en.wikipedia.org/wiki/Architecture_of_Karnataka",
            "The antiquity of architecture of Karnataka is traced to its southern Neolithic and beyond.",
        ),
        card(
            "Karnataka",
            "https://en.wikipedia.org/wiki/Karnataka",
            "Karnataka is a state in south western India.",
        ),
    ]

    ranked = blender().rank("karnataka", cards)

    assert ranked[0].card.title == "Karnataka"
    assert ranked[0].score == 1.0


@pytest.mark.asyncio
async def test_an_exact_title_becomes_the_primary_card():
    cards = [
        card("Architecture of Karnataka", "https://a.in/arch", "architecture of Karnataka traced"),
        card("Karnataka", "https://b.in/ka", "Karnataka is a state in south western India."),
    ]

    out = await blender().blend("karnataka", [], lambda: cards_now(cards))

    assert out["primary_card"]["title"] == "Karnataka"
    assert out["knowledge_cards"][0]["title"] == "Karnataka"


@pytest.mark.asyncio
async def test_the_match_ignores_question_words():
    cards = [card("Karnataka", "https://b.in/ka", "Karnataka is a state in south western India.")]

    out = await blender().blend("what is karnataka", [], lambda: cards_now(cards))

    assert out["primary_card"]["title"] == "Karnataka"


@pytest.mark.asyncio
async def test_the_match_ignores_case_and_spacing():
    cards = [card("  KARNATAKA  ", "https://b.in/ka", "Karnataka is a state in south western India.")]

    out = await blender().blend("karnataka", [], lambda: cards_now(cards))

    assert out["knowledge_cards"][0]["relevance"] == 1.0


def test_without_an_exact_match_the_best_card_wins():
    cards = [
        card("Economy of Karnataka", "https://a.in/e", "Karnataka economy in brief"),
        card("History of Karnataka", "https://b.in/h", "Karnataka history from the empires onward"),
    ]

    ranked = blender().rank("karnataka history", cards)

    assert ranked[0].card.title == "History of Karnataka"


def test_the_ceiling_is_three():
    from app.agents.federated.knowledge_blender import DEFAULT_CARD_CEILING

    assert DEFAULT_CARD_CEILING == 3


@pytest.mark.asyncio
async def test_never_more_than_three_cards():
    cards = [card(f"Karnataka topic {i}", f"https://a{i}.in/x", "Karnataka topic text") for i in range(10)]

    out = await blender().blend("karnataka", [], lambda: cards_now(cards))

    assert len(out["knowledge_cards"]) <= 3


def test_the_full_snippet_buffer_holds_a_paragraph():
    from app.agents.federated.knowledge_courier import SNIPPET_FULL_CHARS

    assert SNIPPET_FULL_CHARS >= 1000
