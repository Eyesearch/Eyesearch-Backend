"""The header set, the Wikidata parser and the optional SearXNG source."""

from __future__ import annotations

import pytest

from app.agents.federated import knowledge_courier as courier
from app.agents.federated import searxng_courier as searxng
from app.agents.federated import wikidata_courier as wikidata
from app.engine.config import Settings


class Http:
    def __init__(self, payloads):
        self.payloads = payloads
        self.urls: list[str] = []

    async def get_json(self, url, timeout=1.5):
        self.urls.append(url)
        for fragment, payload in self.payloads.items():
            if fragment in url:
                return payload
        return {}


def settings(**overrides) -> Settings:
    base = dict(environment="test")
    base.update(overrides)
    return Settings(_env_file=None, **base)


# ---------------------------------------------------------------------------
# Part 1 — the header set
# ---------------------------------------------------------------------------


def test_the_agent_names_a_contact_route():
    """Wikimedia refuses a bare bot name; the address is the requirement."""
    assert "@" in courier.DEFAULT_HEADERS["User-Agent"]
    assert "eyesearch.in" in courier.DEFAULT_HEADERS["User-Agent"]


def test_the_headers_accept_json_and_a_fallback():
    accept = courier.DEFAULT_HEADERS["Accept"]

    assert "application/json" in accept
    assert "*/*" in accept


def test_the_headers_ask_for_a_language():
    assert "en" in courier.DEFAULT_HEADERS["Accept-Language"]


def test_the_client_sends_the_whole_header_set(monkeypatch):
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    courier.FreeKnowledgeCourierAgent()._ensure_client()

    assert captured["headers"] == courier.DEFAULT_HEADERS
    assert captured["follow_redirects"] is False


# ---------------------------------------------------------------------------
# Part 2 — Wikidata
# ---------------------------------------------------------------------------


def test_an_entity_becomes_a_card():
    card = wikidata.card_from_entity(
        {"id": "Q1185", "label": "Karnataka", "description": "state in southern India"}
    )

    assert card.source == "wikidata"
    assert card.title == "Karnataka"
    assert card.extract == "state in southern India"
    assert card.url == "https://www.wikidata.org/wiki/Q1185"


def test_an_entity_without_an_id_is_not_a_card():
    assert wikidata.card_from_entity({"label": "Something"}) is None


def test_an_entity_with_nothing_to_say_is_not_a_card():
    assert wikidata.card_from_entity({"id": "Q1"}) is None


@pytest.mark.asyncio
async def test_the_courier_parses_a_search_response():
    http = Http(
        {
            "wikidata": {
                "search": [
                    {"id": "Q1185", "label": "Karnataka", "description": "state in southern India"},
                    {"id": "Q2", "label": "Karnataka Bank", "description": "an Indian bank"},
                ]
            }
        }
    )

    cards = await wikidata.WikidataCourier(http=http).search("karnataka")

    assert len(cards) == 2
    assert all(card.source == "wikidata" for card in cards)


@pytest.mark.asyncio
async def test_a_failing_wikidata_is_not_fatal():
    class Broken:
        async def get_json(self, url, timeout=1.5):
            raise RuntimeError("down")

    assert await wikidata.WikidataCourier(http=Broken()).search("x") == []


# ---------------------------------------------------------------------------
# Part 3 — SearXNG
# ---------------------------------------------------------------------------


def test_a_result_becomes_a_card():
    card = searxng.card_from_result(
        {"url": "https://ksrtc.in/", "title": "KSRTC", "content": "bus service", "engine": "google"}
    )

    assert card.source == "searxng"
    assert card.url == "https://ksrtc.in/"
    assert card.brand_name == "Google"


def test_a_result_without_a_url_is_not_a_card():
    assert searxng.card_from_result({"title": "nothing"}) is None


@pytest.mark.asyncio
async def test_an_unconfigured_instance_makes_no_request():
    """A missing base URL is a normal install, not a fault."""
    http = Http({})

    cards = await searxng.SearXNGOCourier(http=http, base_url="").search("karnataka")

    assert cards == []
    assert http.urls == []


@pytest.mark.asyncio
async def test_no_client_also_means_no_request():
    assert await searxng.SearXNGOCourier(http=None, base_url="http://searxng:8080").search("x") == []


@pytest.mark.asyncio
async def test_a_configured_instance_is_queried():
    http = Http({"searxng": {"results": [{"url": "https://a.in/x", "title": "A", "content": "text"}]}})

    cards = await searxng.SearXNGOCourier(http=http, base_url="http://searxng:8080").search("karnataka")

    assert len(cards) == 1
    assert http.urls and "searxng:8080" in http.urls[0]


@pytest.mark.asyncio
async def test_a_failing_instance_is_not_fatal():
    class Broken:
        async def get_json(self, url, timeout=1.5):
            raise RuntimeError("unreachable")

    courier_ = searxng.SearXNGOCourier(http=Broken(), base_url="http://searxng:8080")

    assert await courier_.search("x") == []


# ---------------------------------------------------------------------------
# Part 4 — the fleet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_four_sources_are_gathered():
    http = Http(
        {
            # Wikidata's endpoint is also an `api.php`; it is matched first so
            # the stub does not answer it with Wikipedia's payload.
            "wikidata": {"search": [{"id": "Q1185", "label": "Karnataka", "description": "state"}]},
            # `generator=search` answers with pages keyed by id, each carrying
            # the article's own lead rather than a search blurb.
            "api.php": {
                "query": {
                    "pages": {
                        "1": {
                            "pageid": 1,
                            "index": 1,
                            "title": "Karnataka",
                            "extract": "Karnataka is a state in south western India.",
                        }
                    }
                }
            },
            "searxng": {"results": [{"url": "https://ksrtc.in/", "title": "KSRTC", "content": "bus"}]},
        }
    )
    agent = courier.FreeKnowledgeCourierAgent(
        http=http, settings=settings(searxng_base_url="http://searxng:8080")
    )

    cards = await agent.search("karnataka")

    assert {"wikipedia", "wikidata", "searxng"} <= {card.source for card in cards}


@pytest.mark.asyncio
async def test_without_searxng_configured_only_three_sources_run():
    http = Http(
        {
            "wikidata": {"search": [{"id": "Q1185", "label": "Karnataka", "description": "state"}]},
            # `generator=search` answers with pages keyed by id, each carrying
            # the article's own lead rather than a search blurb.
            "api.php": {
                "query": {
                    "pages": {
                        "1": {
                            "pageid": 1,
                            "index": 1,
                            "title": "Karnataka",
                            "extract": "Karnataka is a state in south western India.",
                        }
                    }
                }
            },
        }
    )
    agent = courier.FreeKnowledgeCourierAgent(http=http, settings=settings())

    cards = await agent.search("karnataka")

    assert "searxng" not in {card.source for card in cards}


def test_the_searxng_setting_defaults_to_empty():
    assert Settings(_env_file=None).searxng_base_url == ""


def test_the_new_couriers_keep_the_air_gap():
    for module in (wikidata, searxng):
        source = open(module.__file__, encoding="utf-8").read()
        for forbidden in ("app.engine.store", "app.postgres", "crawl_queue", "INSERT INTO"):
            assert forbidden not in source


# ---------------------------------------------------------------------------
# The badge, the link and the width
# ---------------------------------------------------------------------------


def test_a_wikipedia_card_is_badged_with_its_source():
    """Deriving a brand from the article title put the title in the badge, so a
    reader saw "Architecture of Karnataka" twice, once in capitals."""
    from app.agents.federated.source_sanitizer import CrossSourceSanitizerAgent

    card = CrossSourceSanitizerAgent().card(
        title="Architecture of Karnataka",
        url="https://en.wikipedia.org/wiki/Architecture_of_Karnataka",
        snippet="The antiquity of architecture of Karnataka is traced to its southern Neolithic.",
        source="wikipedia",
    )

    assert card.brand == "Wikipedia"
    assert card.brand.lower() != card.title.lower()


def test_every_known_source_names_itself():
    from app.agents.federated.source_sanitizer import CrossSourceSanitizerAgent

    agent = CrossSourceSanitizerAgent()
    for source, label in agent.SOURCE_LABELS.items():
        card = agent.card(
            title="Something Else Entirely",
            url="https://example.in/x",
            snippet="text",
            source=source,
        )
        assert card.brand == label, source


def test_an_unknown_source_still_derives_a_brand():
    from app.agents.federated.source_sanitizer import CrossSourceSanitizerAgent

    card = CrossSourceSanitizerAgent().card(
        title="Karnataka Bank", url="https://karnatakabank.com/", snippet="text", source=""
    )

    assert card.brand


def test_the_article_link_keeps_its_path():
    """The card must link to the article, not to the domain."""
    from app.agents.federated.source_sanitizer import CrossSourceSanitizerAgent

    url = "https://en.wikipedia.org/wiki/Great_Indian_Railway_Journeys"
    card = CrossSourceSanitizerAgent().card(title="T", url=url, snippet="s", source="wikipedia")

    assert card.url == url
    assert "/wiki/" in card.url


def test_the_full_snippet_holds_a_lead_paragraph():
    from app.agents.federated.source_sanitizer import SNIPPET_FULL_CHARS

    assert SNIPPET_FULL_CHARS >= 1000


def test_a_long_lead_is_not_cut_at_six_hundred():
    from app.agents.federated.source_sanitizer import CrossSourceSanitizerAgent

    lead = (
        "Karnataka is a state in the south western region of India. It was formed "
        "on 1 November 1956, with the passing of the States Reorganisation Act, and "
        "it was originally known as the State of Mysore. It was renamed in 1973. "
    ) * 3
    card = CrossSourceSanitizerAgent().card(
        title="Karnataka", url="https://en.wikipedia.org/wiki/Karnataka",
        snippet=lead, source="wikipedia",
    )

    assert len(card.snippet_full) > 600
    assert len(card.snippet_full) <= 1200


def test_a_cut_never_ends_on_a_word_that_cannot_end_a_sentence():
    from app.agents.federated.source_sanitizer import sanitise_snippet

    text = (
        "The antiquity of architecture of Karnataka is traced to its southern "
        "Neolithic and beyond the recorded history of the region"
    )
    cut = sanitise_snippet(text, 80)

    assert not cut.lower().endswith((" and", " of", " in", " to", " for", " with"))
    assert cut.lower().endswith("neolithic"), cut


def test_a_short_snippet_is_untouched():
    from app.agents.federated.source_sanitizer import sanitise_snippet

    assert sanitise_snippet("A short line.", 1200) == "A short line."


def test_the_snippet_width_is_not_the_storage_ceiling():
    """Two unrelated policies: how much of an external article we show, and how
    much of a crawled page we store. Raising one must not move the other."""
    from app.agents.federated import source_sanitizer

    assert source_sanitizer.SNIPPET_CHARS != 2048
    assert source_sanitizer.SNIPPET_CHARS >= 1000


# ---------------------------------------------------------------------------
# The Wikipedia lead paragraph
# ---------------------------------------------------------------------------

LEAD = (
    "Mysore Palace, also known as Amba Vilas Palace, is a historical palace and a "
    "royal residence at Mysore in the Indian State of Karnataka. It is the official "
    "residence of the Wadiyar dynasty and the seat of the Kingdom of Mysore. The "
    "palace is in the centre of Mysore, and faces the Chamundi Hills eastward."
)


class Wikipedia:
    """Answers the generator query the way the real endpoint does."""

    def __init__(self, pages):
        self.pages = pages
        self.urls: list[str] = []

    async def get_json(self, url, timeout=1.5):
        self.urls.append(url)
        return {"query": {"pages": self.pages}}


@pytest.mark.asyncio
async def test_the_card_carries_the_article_lead_not_the_search_blurb():
    """`list=search` returns a hundred and fifty character blurb; the article's
    lead is what a reader needs, and it is a different field."""
    http = Wikipedia(
        {
            "1": {
                "pageid": 1, "index": 1, "title": "Mysore Palace",
                "extract": LEAD, "snippet": "a palace in Mysore",
            }
        }
    )

    cards = await courier.FreeKnowledgeCourierAgent(http=http).search_wikipedia("Mysore Palace")

    assert len(cards) == 1
    assert len(cards[0].snippet_full) > 220, len(cards[0].snippet_full)
    assert cards[0].snippet_full.startswith("Mysore Palace, also known as")


@pytest.mark.asyncio
async def test_the_lead_is_fetched_in_one_request():
    """The REST summary returns the same lead for one title, so using it per
    result would be one request each."""
    http = Wikipedia({"1": {"pageid": 1, "index": 1, "title": "T", "extract": LEAD}})

    await courier.FreeKnowledgeCourierAgent(http=http).search_wikipedia("t")

    assert len(http.urls) == 1
    assert "prop=extracts" in http.urls[0]
    assert "generator=search" in http.urls[0]


@pytest.mark.asyncio
async def test_the_brand_and_source_are_named():
    http = Wikipedia({"1": {"pageid": 1, "index": 1, "title": "Mysore Palace", "extract": LEAD}})

    cards = await courier.FreeKnowledgeCourierAgent(http=http).search_wikipedia("Mysore Palace")

    assert cards[0].brand_name == "Wikipedia"
    assert cards[0].source == "wikipedia"


@pytest.mark.asyncio
async def test_the_link_is_the_article_not_the_domain():
    http = Wikipedia({"1": {"pageid": 1, "index": 1, "title": "Mysore Palace", "extract": LEAD}})

    cards = await courier.FreeKnowledgeCourierAgent(http=http).search_wikipedia("Mysore Palace")

    assert cards[0].url == "https://en.wikipedia.org/wiki/Mysore_Palace"


@pytest.mark.asyncio
async def test_a_page_without_a_lead_falls_back_to_the_blurb():
    http = Wikipedia({"1": {"pageid": 1, "index": 1, "title": "Thin", "snippet": "a short blurb"}})

    cards = await courier.FreeKnowledgeCourierAgent(http=http).search_wikipedia("thin")

    assert cards[0].snippet_full == "a short blurb"


@pytest.mark.asyncio
async def test_the_search_ranking_order_is_restored():
    """The generator answers with a mapping keyed by page id, which does not
    preserve the ranking. `index` does."""
    http = Wikipedia(
        {
            "2": {"pageid": 2, "index": 2, "title": "Second", "extract": "second lead"},
            "1": {"pageid": 1, "index": 1, "title": "First", "extract": "first lead"},
        }
    )

    cards = await courier.FreeKnowledgeCourierAgent(http=http).search_wikipedia("q")

    assert [c.title for c in cards] == ["First", "Second"]
