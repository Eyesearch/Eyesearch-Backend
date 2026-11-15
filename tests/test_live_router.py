"""The router, and the one-answer endpoint built on it.

The router is the difference between a live layer that exists and a live layer
that is used: a caller asks "what is the gold rate", not "gold.22k.inr.gram".
"""

from __future__ import annotations

import pytest

from app.live import store as live
from app.live.compose import from_index, from_live
from app.live.router import route


@pytest.mark.parametrize(
    "question,kind,key",
    [
        ("today gold price", "bullion", "gold.22k.inr.gram"),
        ("24k gold rate per 10 gram", "bullion", "gold.24k.inr.10g"),
        ("18k gold rate", "bullion", "gold.18k.inr.gram"),
        ("silver rate per kg", "bullion", "silver.inr.kg"),
        ("silver price", "bullion", "silver.inr.gram"),
        ("weather in bangalore", "weather", "weather.bengaluru.temperature"),
        ("mausam in mumbai", "weather", "weather.mumbai.temperature"),
        ("latest news", "news", None),
        ("karnataka scheme status", "index", None),
    ],
)
def test_a_question_is_routed_to_the_layer_that_can_answer_it(question, kind, key):
    routed = route(question)
    assert routed.kind == kind
    assert routed.key == key


def test_a_weather_question_about_a_city_we_do_not_fill_is_not_guessed_at():
    """Answering with some other city's temperature would be worse than a page.

    London is filled now that the feed is worldwide, so the example is a place
    that is not a city at all.
    """
    assert route("weather in the sahara desert").kind == "index"


@pytest.mark.parametrize(
    "question,city",
    [
        ("weather in shimoga", "shivamogga"),
        ("weather in hubli", "hubballi"),
        ("weather in mangalore", "mangaluru"),
        ("weather in vizag", "visakhapatnam"),
        ("weather in guwahati", "guwahati"),
    ],
)
def test_a_city_is_matched_by_the_name_people_use(question, city):
    """"shimoga" is how people write Shivamogga, and the feed stores the latter."""
    assert route(question).key == f"weather.{city}.temperature"


def test_a_kannada_question_is_understood():
    assert route("ಚಿನ್ನದ ಬೆಲೆ").kind == "bullion"


@pytest.mark.parametrize(
    "question,key",
    [
        ("gold price in dubai", "gold.spot.aed.gram"),
        ("gold price in dollars", "gold.spot.usd.gram"),
        ("gold rate in london", "gold.spot.gbp.gram"),
        ("gold price in india", "gold.22k.inr.gram"),
        ("22k gold rate", "gold.22k.inr.gram"),
    ],
)
def test_gold_is_answered_in_the_currency_the_question_names(question, key):
    """IBJA quotes the Indian retail rate; everywhere else is the spot price."""
    assert route(question).key == key


@pytest.mark.parametrize(
    "question,edition",
    [("us news", "us"), ("uk news", "gb"), ("kannada news", "in_kn")],
)
def test_a_news_question_can_name_its_country(question, edition):
    routed = route(question)
    assert routed.kind == "news" and routed.key == edition


@pytest.mark.asyncio
async def test_a_bullion_question_gets_the_stored_value(client):
    await live.put_fact(client, "gold.22k.inr.gram", "12440.1", unit="INR/gram", source="bullion")

    payload = await from_live(client, route("today gold price"))

    assert payload is not None
    assert payload["answer"] == "12440.1 INR/gram"
    assert payload["freshness"] == "live"
    assert payload["as_of"]


@pytest.mark.asyncio
async def test_a_news_question_gets_the_headlines(client):
    await live.put_fact(client, "news.pib.1", "Cabinet approves scheme", unit="https://pib.gov.in/1")
    await live.put_fact(client, "news.pib.2", "Second headline", unit="https://pib.gov.in/2")

    payload = await from_live(client, route("latest news"))

    assert payload is not None
    assert payload["kind"] == "news"
    assert payload["count"] == 2


@pytest.mark.asyncio
async def test_a_question_with_no_live_answer_says_it_is_from_the_index(client):
    payload = from_index(
        [{"title": "Karnataka scheme", "url": "https://karnataka.gov.in/x", "snippet": "s"}],
        "karnataka scheme status",
    )

    assert payload["freshness"] == "indexed"
    assert payload["answer"] == "Karnataka scheme"
    assert "crawled" in payload["note"]


def test_an_unanswered_question_is_missing_not_invented():
    payload = from_index([], "what is the population of atlantis")
    assert payload["freshness"] == "missing"
    assert payload["answer"] is None
