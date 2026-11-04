"""Multi-result search: both sources, the ceiling, and what must not regress."""

from __future__ import annotations

import pytest

from app.agents.federated import knowledge_courier as courier

Agent = courier.FreeKnowledgeCourierAgent


class TwoSources:
    """Wikipedia answers with n results, DuckDuckGo with m."""

    def __init__(self, wiki: int = 15, ddg: int = 10, url_prefix: str = "https://ex"):
        self.wiki, self.ddg, self.prefix = wiki, ddg, url_prefix
        self.seen: list[str] = []

    async def get_json(self, url, timeout=1.5):
        self.seen.append("wiki" if "api.php" in url else "ddg")
        if "api.php" in url:
            return {
                "query": {
                    "search": [
                        {"title": f"Article {i}", "snippet": f"<span>snippet</span> {i}"}
                        for i in range(self.wiki)
                    ]
                }
            }
        return {
            "RelatedTopics": [
                {"Topics": [{"FirstURL": f"{self.prefix}{i}.in/x", "Text": f"T{i} - body {i}"}]}
                for i in range(self.ddg)
            ]
        }


# ---------------------------------------------------------------------------
# Dynamic results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_sources_are_queried_concurrently():
    http = TwoSources()
    cards = await Agent(http=http).search("q")

    # Wikipedia and DuckDuckGo always; Wikidata too when a client is injected.
    assert "wiki" in http.seen and "ddg" in http.seen
    assert {card.source for card in cards} == {"wikipedia", "duckduckgo"}


@pytest.mark.asyncio
async def test_everything_that_survives_is_returned_up_to_the_ceiling():
    cards = await Agent(http=TwoSources(wiki=15, ddg=10)).search("q")

    # 25 candidates, ceiling of 20.
    assert len(cards) == courier.MAX_CARDS == 20


@pytest.mark.asyncio
async def test_a_smaller_answer_is_not_padded():
    cards = await Agent(http=TwoSources(wiki=3, ddg=2)).search("q")

    assert len(cards) == 5


@pytest.mark.asyncio
async def test_the_ceiling_can_be_lowered_by_the_caller():
    cards = await Agent(http=TwoSources()).search("q", limit=4)

    assert len(cards) == 4


@pytest.mark.asyncio
async def test_a_duplicate_url_appears_once():
    class Dupes:
        async def get_json(self, url, timeout=1.5):
            if "api.php" in url:
                return {"query": {"search": [{"title": "A", "snippet": "x"}]}}
            return {"RelatedTopics": [{"FirstURL": "https://en.wikipedia.org/wiki/A", "Text": "A - y"}]}

    cards = await Agent(http=Dupes()).search("q")

    assert len(cards) == 1


@pytest.mark.asyncio
async def test_one_source_failing_does_not_lose_the_other():
    class HalfBroken:
        async def get_json(self, url, timeout=1.5):
            if "api.php" in url:
                raise RuntimeError("wikipedia down")
            return {"RelatedTopics": [{"FirstURL": "https://a.in/x", "Text": "T - body"}]}

    cards = await Agent(http=HalfBroken()).search("q")

    assert len(cards) == 1
    assert cards[0].source == "duckduckgo"


# ---------------------------------------------------------------------------
# Buffers
# ---------------------------------------------------------------------------


def test_the_buffers_are_the_agreed_widths():
    assert courier.TITLE_CHARS == 100
    assert courier.SNIPPET_SHORT_CHARS == 300
    # Wide enough to hold a Wikipedia lead paragraph whole, so the toggle
    # reveals an answer rather than a fragment.
    assert courier.SNIPPET_FULL_CHARS == 1200


@pytest.mark.asyncio
async def test_a_long_snippet_is_cut_at_a_word_boundary():
    class Long:
        async def get_json(self, url, timeout=1.5):
            if "api.php" in url:
                return {"query": {"search": [{"title": "T", "snippet": "word " * 200}]}}
            return {}

    card = (await Agent(http=Long()).search("q"))[0]

    assert len(card.extract) <= courier.SNIPPET_SHORT_CHARS
    assert not card.extract.endswith("wor")
    assert len(card.snippet_full) <= courier.SNIPPET_FULL_CHARS


# ---------------------------------------------------------------------------
# Sanitising
# ---------------------------------------------------------------------------


def test_wikipedia_searchmatch_markup_is_stripped():
    assert courier.sanitize_text('<span class="searchmatch">Bengaluru</span> is') == "Bengaluru is"


def test_entities_are_decoded_before_tags_are_stripped():
    """`&lt;b&gt;` is not a tag until it is decoded, so unescaping runs first
    and the tag it becomes is then removed like any other."""
    assert courier.sanitize_text("&lt;b&gt;bold&lt;/b&gt; &amp; clean") == "bold & clean"


def test_emoji_go_but_the_rupee_sign_stays():
    cleaned = courier.sanitize_text("😀 Fee is ₹500 – payable")

    assert "😀" not in cleaned
    assert "₹" in cleaned
    assert "–" in cleaned


def test_newlines_and_tabs_collapse():
    assert courier.sanitize_text("a\n\n\tb   c") == "a b c"


# ---------------------------------------------------------------------------
# What must NOT have regressed
# ---------------------------------------------------------------------------


def test_the_ssrf_guard_is_still_the_real_one():
    """The supplied rewrite replaced it with a four-string substring check."""
    assert courier.is_public_url("http://10.1.2.3/x", resolve=False)[0] is False
    assert courier.is_public_url("http://172.16.0.1/x", resolve=False)[0] is False
    assert courier.is_public_url("http://169.254.169.254/x", resolve=False)[0] is False


def test_downloads_and_login_pages_are_not_cards():
    assert courier.is_cardable_url("https://a.in/report.pdf") is False
    assert courier.is_cardable_url("https://a.in/login") is False
    assert courier.is_cardable_url("https://a.in/wp-admin/") is False


def test_a_word_containing_admin_is_not_a_login_page():
    """A substring test would reject this; a segment test does not."""
    assert courier.is_cardable_url("https://a.in/administrative-reforms") is True


def test_an_empty_card_is_still_falsy():
    """The supplied rewrite dropped __bool__, which made empty cards truthy."""
    assert bool(courier.KnowledgeCard()) is False
    assert bool(courier.KnowledgeCard(title="T", url="https://a.in", extract="text")) is True


def test_the_contract_still_exists():
    assert courier.CONTRACT["agent"] == "federated.knowledge_courier"
    courier.audit_contract()


@pytest.mark.asyncio
async def test_lookup_still_falls_back_to_duckduckgo():
    class EmptyWiki:
        async def get_json(self, url, timeout=1.5):
            if "api.php" in url or "rest_v1" in url:
                return {}
            return {"Heading": "H", "AbstractText": "A", "AbstractURL": "https://a.in/x"}

    card = await Agent(http=EmptyWiki()).lookup("q")

    assert card.source == "duckduckgo"
    assert card.extract == "A"


def test_the_air_gap_is_intact():
    source = open(courier.__file__, encoding="utf-8").read()
    for forbidden in ("app.engine.store", "app.postgres", "crawl_queue", "INSERT INTO"):
        assert forbidden not in source
