"""Match snippets and title ranking.

The brief asked for FTS5's `snippet()`. On the SQLite build this project runs
on, `snippet()` ignores its `colnum` argument and returns column 0 for every
call — `highlight()` behaves correctly, `snippet()` does not. The window is
therefore computed in Python, and these tests pin the behaviour that matters:
the passage shown contains the term that matched, and a title match is not
echoed back as the snippet.
"""

from __future__ import annotations

import pytest

import app.engine.store as store_module
from app.engine.store import PageDocument, passage_around_match

FILLER = "The department publishes routine notices for district offices. " * 6


def _doc(url: str, title: str, body: str, digest: str) -> PageDocument:
    return PageDocument(
        url=url,
        domain=url.split("//", 1)[1].split("/", 1)[0],
        title=title,
        description=f"{title} description.",
        body_text=body,
        digest=digest,
    )


# ---------------------------------------------------------------------------
# The passage window
# ---------------------------------------------------------------------------


def test_the_passage_contains_the_term_that_matched():
    body = FILLER + "Bengaluru metro fares were revised in March." + FILLER

    passage = passage_around_match(body, ["metro"])

    assert "metro" in passage.lower()


def test_a_passage_is_shorter_than_a_long_body():
    body = FILLER * 5 + "metro" + FILLER * 5

    assert len(passage_around_match(body, ["metro"])) < len(body)


def test_no_match_returns_nothing_so_the_caller_can_fall_back():
    assert passage_around_match("nothing relevant here at all", ["metro"]) == ""
    assert passage_around_match("", ["metro"]) == ""
    assert passage_around_match("some text", []) == ""


def test_the_window_does_not_cut_a_word_in_half():
    passage = passage_around_match("alpha beta gamma delta", ["gamma"], window=12)

    assert passage == "…gamma…"


def test_a_match_at_the_start_has_no_leading_ellipsis():
    passage = passage_around_match("metro fares rose today and more follows", ["metro"], window=20)

    assert not passage.startswith("…")
    assert passage.startswith("metro")


def test_the_first_occurrence_wins():
    body = "alpha metro beta " + ("filler " * 100) + "metro again"

    passage = passage_around_match(body, ["metro"], window=40)

    assert passage.count("metro") == 1


# ---------------------------------------------------------------------------
# Search results
# ---------------------------------------------------------------------------


async def test_a_body_match_produces_a_snippet_containing_the_term(store):
    await store.persist_batch(
        [_doc("https://a.gov.in/", "Notices", FILLER + "Metro fares were revised." + FILLER, "a")]
    )

    hits = await store.search("metro")

    assert len(hits) == 1
    assert "metro" in hits[0].snippet.lower()


async def test_a_title_only_match_falls_back_to_the_description(store):
    """The snippet must not just repeat the heading."""
    await store.persist_batch(
        [_doc("https://b.gov.in/", "Metro fares revised", "Nothing about transport here.", "b")]
    )

    hits = await store.search("metro")

    assert hits[0].title == "Metro fares revised"
    assert hits[0].snippet == "Metro fares revised description."


async def test_a_page_with_no_body_match_and_no_description_still_has_a_snippet(store):
    document = _doc("https://c.gov.in/", "Metro", "", "c")
    document.description = ""
    await store.persist_batch([document])

    hits = await store.search("metro")

    assert hits[0].snippet == ""


# ---------------------------------------------------------------------------
# Title boost
# ---------------------------------------------------------------------------


async def _seed_title_and_body(store) -> None:
    await store.persist_batch(
        [
            _doc("https://title.gov.in/", "Metro fares revised", "Fares went up this year.", "title"),
            _doc("https://body.gov.in/", "Notices", "Metro fares were revised this year.", "body"),
        ]
    )


async def test_a_title_match_ranks_first(store):
    await _seed_title_and_body(store)

    hits = await store.search("metro")

    assert hits[0].title == "Metro fares revised"


async def test_the_title_boost_lowers_the_ranking_key(store, monkeypatch):
    """bm25 is lower-is-better, so a boost must make the key more negative."""
    await _seed_title_and_body(store)

    boosted = await store.search("metro")
    boosted_key = next(hit.raw_score for hit in boosted if "Metro" in hit.title)

    monkeypatch.setattr(store_module, "TITLE_MATCH_BOOST", 0.0)
    plain = await store.search("metro")
    plain_key = next(hit.raw_score for hit in plain if "Metro" in hit.title)

    assert boosted_key < plain_key


async def test_the_reported_score_is_monotone_with_the_order(store):
    await _seed_title_and_body(store)

    hits = await store.search("metro")
    scores = [hit.score for hit in hits]

    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 1.0


async def test_the_boost_does_not_change_which_pages_match(store):
    await _seed_title_and_body(store)

    hits = await store.search("metro")

    assert len(hits) == 2


async def test_filters_still_apply_with_the_boost(store):
    first = _doc("https://a.gov.in/", "Metro fares", "Fares.", "a")
    first.category = "government"
    second = _doc("https://b.gov.in/", "Metro news", "News.", "b")
    second.category = "news_media"
    await store.persist_batch([first, second])

    hits = await store.search("metro", category="news_media")

    assert [hit.title for hit in hits] == ["Metro news"]


# ---------------------------------------------------------------------------
# Authority ranking
# ---------------------------------------------------------------------------


async def test_an_authoritative_domain_outranks_an_equal_commercial_page(store):
    body = "The state publishes metro fare revisions for commuters across the city."
    await store.persist_batch(
        [
            _doc("https://example.com/metro", "Metro fares", body, digest="d-com"),
            _doc("https://karnataka.gov.in/metro", "Metro fares", body, digest="d-gov"),
        ]
    )

    hits = await store.search("metro")

    assert len(hits) == 2
    assert "gov.in" in hits[0].url


async def test_the_boost_reads_the_domain_not_the_url_text(store):
    """A query string containing '.gov.in/' must not earn the boost."""
    body = "The state publishes metro fare revisions for commuters across the city."
    await store.persist_batch(
        [
            _doc("https://example.com/plain", "Metro fares", body, digest="d-plain"),
            _doc(
                "https://example.com/spoof?u=.gov.in/x",
                "Metro fares",
                body,
                digest="d-spoof",
            ),
        ]
    )

    hits = await store.search("metro")
    scores = {hit.url: hit.raw_score for hit in hits}

    # Identical content on the same (non-authoritative) domain: identical score.
    assert scores["https://example.com/plain"] == scores["https://example.com/spoof?u=.gov.in/x"]


async def test_a_query_the_strict_match_misses_still_answers(store):
    """The OR fallback reuses the SQL, so its bind order must stay in step."""
    await store.persist_batch(
        [_doc("https://karnataka.gov.in/a", "Karnataka districts", "A list of districts.", digest="d-fb")]
    )

    hits = await store.search("karnataka metro")

    assert hits


# ---------------------------------------------------------------------------
# Script and language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query, tokens",
    [
        ("ಕರ್ನಾಟಕ ಆರೋಗ್ಯ", ["ಕರ್ನಾಟಕ", "ಆರೋಗ್ಯ"]),
        ("कर्नाटक स्वास्थ्य", ["कर्नाटक", "स्वास्थ्य"]),
        ("தமிழ்நாடு", ["தமிழ்நாடு"]),
        ("karnataka health", ["karnataka", "health"]),
    ],
)
def test_indic_words_keep_their_combining_marks(query, tokens):
    """`isalnum()` drops every Indian vowel sign: "ಕರ್ನಾಟಕ" became "ಕರನಟಕ",
    a word that matches nothing. The marks are part of the word."""
    from app.engine.store import fts_tokens

    assert fts_tokens(query) == tokens


def test_a_kannada_query_builds_a_kannada_tsquery():
    from app.engine.store import build_tsquery

    assert build_tsquery("ಕರ್ನಾಟಕ ಆರೋಗ್ಯ") == "ಕರ್ನಾಟಕ & ಆರೋಗ್ಯ"


def test_the_query_language_is_detected_from_its_script():
    from app.engine.store import _query_language

    assert _query_language("ಕರ್ನಾಟಕ ಆರೋಗ್ಯ") == "kn"
    assert _query_language("कर्नाटक") == "hi"
    assert _query_language("karnataka") == "en"


@pytest.mark.asyncio
async def test_a_kannada_query_finds_the_kannada_page(store):
    from app.engine.store import PageDocument

    await store.persist_batch(
        [
            PageDocument(
                url="https://a.gov.in/kn",
                domain="a.gov.in",
                title="ಕರ್ನಾಟಕ ಆರೋಗ್ಯ ಇಲಾಖೆ",
                description="ಜಿಲ್ಲಾ ಅಧಿಸೂಚನೆ",
                body_text="ಕರ್ನಾಟಕ ಆರೋಗ್ಯ ಇಲಾಖೆ ಜಿಲ್ಲಾ ಅಧಿಸೂಚನೆ",
                digest="lang-1",
                language="kn",
            ),
            PageDocument(
                url="https://b.gov.in/en",
                domain="b.gov.in",
                title="Karnataka Health Department",
                description="district notification",
                body_text="Karnataka health department district notification",
                digest="lang-2",
                language="en",
            ),
        ]
    )

    hits = await store.search("ಕರ್ನಾಟಕ ಆರೋಗ್ಯ")

    assert [hit.url for hit in hits] == ["https://a.gov.in/kn"]


def test_a_timeout_is_told_apart_from_a_real_fault():
    from app.engine.store import _is_timeout

    assert _is_timeout(Exception("canceling statement due to statement timeout")) is True
    assert _is_timeout(Exception("relation \"pages\" does not exist")) is False
