"""Multi-card aggregation: dedupe, richness, ceiling, and what must not merge."""

from __future__ import annotations

import pytest

from app.agents.federated import knowledge_courier as courier
from app.agents.federated import brand_extractor_agent as brands
from app.agents.federated import source_sanitizer as sanitizer

Agent = sanitizer.CrossSourceSanitizerAgent
Card = courier.KnowledgeCard


def card(title, url, text="body text", full=None):
    return Card(title=title, url=url, extract=text, snippet_full=full if full is not None else text)


# ---------------------------------------------------------------------------
# Dynamic results
# ---------------------------------------------------------------------------


def test_everything_that_survives_is_returned_up_to_the_ceiling():
    cards = [card(f"Article number {i}", f"https://a{i}.in/x") for i in range(25)]

    out = Agent().aggregate(cards)

    assert len(out) == sanitizer.MAX_CARDS == 20


def test_a_smaller_answer_is_not_padded():
    cards = [card(f"Article number {i}", f"https://a{i}.in/x") for i in range(6)]

    assert len(Agent().aggregate(cards)) == 6


def test_the_ceiling_can_be_lowered():
    cards = [card(f"Article number {i}", f"https://a{i}.in/x") for i in range(10)]

    assert len(Agent().aggregate(cards, limit=3)) == 3


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


def test_the_same_url_twice_is_one_card():
    cards = [card("Page one", "https://a.in/x"), card("Page one", "https://a.in/x#frag")]

    assert len(Agent().aggregate(cards)) == 1


def test_the_richer_of_two_duplicates_wins():
    cards = [
        card("Page one", "https://a.in/x", "short"),
        card("Page one", "https://a.in/x", "a much longer body of text"),
    ]

    out = Agent().aggregate(cards)

    assert len(out) == 1
    assert out[0].snippet_short == "a much longer body of text"


def test_two_pages_with_a_generic_title_stay_separate():
    """Merging on "Home" would collapse two different sites into one card."""
    cards = [card("Home", "https://a.in/"), card("Home", "https://b.in/")]

    assert len(Agent().aggregate(cards)) == 2


def test_two_pages_with_a_distinctive_title_merge():
    title = "District Health Notification For November 2026"
    cards = [card(title, "https://a.in/x"), card(title, "https://a.in/y")]

    assert len(Agent().aggregate(cards)) == 1


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["https://a.in/report.pdf", "https://a.in/login", "https://a.in/wp-admin/"]
)
def test_a_download_or_a_door_is_not_a_card(url):
    assert Agent().aggregate([card("Something", url)]) == []


def test_a_word_containing_admin_is_still_a_card():
    out = Agent().aggregate([card("Reforms", "https://a.in/administrative-reforms")])

    assert len(out) == 1


def test_a_private_address_is_not_a_card():
    assert Agent().aggregate([card("Internal", "http://127.0.0.1/x")]) == []


def test_a_javascript_url_is_not_a_card():
    assert Agent().aggregate([card("Bad", "javascript:alert(1)")]) == []


# ---------------------------------------------------------------------------
# Buffers and text
# ---------------------------------------------------------------------------


def test_the_buffers_are_the_agreed_widths():
    assert sanitizer.TITLE_CHAR_CAP == 100
    assert sanitizer.SNIPPET_SHORT_CHARS == 300
    assert sanitizer.SNIPPET_FULL_CHARS == 1200
    assert brands.BRAND_CHAR_CAP == 50


def test_a_long_body_is_cut_at_a_word_boundary():
    out = Agent().aggregate([card("T", "https://a.in/x", "word " * 300)])

    assert len(out[0].snippet_short) <= sanitizer.SNIPPET_SHORT_CHARS
    assert not out[0].snippet_short.endswith("wor")
    assert len(out[0].snippet_full) <= sanitizer.SNIPPET_FULL_CHARS


def test_markup_and_emoji_are_cleaned():
    out = Agent().aggregate(
        [card("<b>Title</b>", "https://a.in/x", "😀 <mark>text</mark> &amp; more")]
    )

    assert "<b>" not in out[0].title
    assert "😀" not in out[0].snippet_short
    assert "<mark>" not in out[0].snippet_short
    assert "&amp;" not in out[0].snippet_short


def test_an_unusable_card_is_skipped_not_fatal():
    cards = [card("Good page here", "https://a.in/x"), card("Bad", ""), card("Also good page", "https://b.in/y")]

    assert len(Agent().aggregate(cards)) == 2


def test_aggregation_never_touches_the_database():
    source = open(sanitizer.__file__, encoding="utf-8").read()
    for forbidden in ("app.engine.store", "app.postgres", "crawl_queue", "INSERT INTO"):
        assert forbidden not in source
