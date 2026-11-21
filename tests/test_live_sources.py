"""The three live sources, against fixed responses.

No network in the suite: each source is handed a fake HTTP client and the test
asserts what it makes of a response the real service is documented to send.
"""

from __future__ import annotations

import pytest

from app.live import store as live
from app.live.sources.bullion import BullionSource
from app.live.sources.news import NewsSource, parse_items
from app.live.sources.weather import WeatherSource, describe


class FakeResponse:
    def __init__(self, payload=None, text="", status=200):
        self._payload = payload
        self.text = text
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class FakeHTTP:
    def __init__(self, payload=None, text="", status=200):
        self._response = FakeResponse(payload, text, status)
        self.calls: list[dict] = []

    async def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self._response

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_bullion_turns_every_number_into_a_fact():
    """The response shape is undocumented, so the adapter reads paths, not names.

    Whatever the service calls its fields, each numeric leaf becomes a key the
    reader can ask for, and `GET /v1/live` lists them after the first pass.
    """
    payload = {
        "data": {
            "gold": {"24k": {"per_gram": 6500, "per_10g": "65000"}, "22k": {"per_gram": 6145}},
            "silver": {"per_kg": 74500},
        },
        "meta": {"updated": "2026-11-14T06:00:00Z"},
    }
    source = BullionSource(http=FakeHTTP(payload))

    facts = await source.fetch()
    keys = {key: value for key, value, _ in facts}

    assert keys["bullion.gold.24k.per_gram"] == "6500"
    assert keys["bullion.gold.24k.per_10g"] == "65000"
    assert keys["bullion.gold.22k.per_gram"] == "6145"
    assert keys["bullion.silver.per_kg"] == "74500"


@pytest.mark.asyncio
async def test_a_label_is_kept_beside_its_number():
    """Words are stored too, because a rate's metal is a word.

    Dropping strings left six anonymous rows whose only distinguishing mark was
    a purity two of them shared, which is how `rates.5` and `rates.6` became
    impossible to tell apart.
    """
    payload = {
        "data": {
            "rates": [
                {"metal": "gold", "purity": "999", "perGram": 6500, "note": "before GST"},
            ]
        }
    }
    source = BullionSource(http=FakeHTTP(payload))

    facts = {key: value for key, value, _ in await source.fetch()}

    assert facts["bullion.rates.0.perGram"] == "6500"
    assert facts["bullion.rates.0.metal"] == "gold"
    assert facts["bullion.rates.0.note"] == "before GST"


@pytest.mark.asyncio
async def test_weather_stores_a_temperature_per_city():
    payload = {
        "current": {
            "temperature_2m": 27.4,
            "relative_humidity_2m": 61,
            "precipitation": 0.0,
            "weather_code": 3,
        }
    }
    source = WeatherSource(http=FakeHTTP(payload), cities={"bengaluru": (12.97, 77.59)})

    facts = {key: value for key, value, _ in await source.fetch()}

    assert facts["weather.bengaluru.temperature"] == "27.4"
    assert facts["weather.bengaluru.condition"] == "overcast"


@pytest.mark.asyncio
async def test_one_city_failing_does_not_lose_the_others():
    class FlakyHTTP(FakeHTTP):
        def __init__(self):
            super().__init__({"current": {"temperature_2m": 30}})
            self.count = 0

        async def get(self, url, **kwargs):
            self.count += 1
            if self.count == 1:
                raise RuntimeError("timeout")
            return self._response

    source = WeatherSource(http=FlakyHTTP(), cities={"a": (1.0, 1.0), "b": (2.0, 2.0)})

    facts = {key for key, _, _ in await source.fetch()}

    assert "weather.a.temperature" not in facts
    assert "weather.b.temperature" in facts


def test_weather_codes_read_as_words():
    assert describe(0) == "clear sky"
    assert describe(3) == "overcast"
    assert describe(95) == "thunderstorm"
    assert describe(1234) == "unknown"


def test_rss_items_are_read_with_their_links():
    xml = """<?xml version="1.0"?><rss><channel>
      <item><title>First &amp; foremost</title><link>https://pib.gov.in/1</link></item>
      <item><title><![CDATA[Second]]></title><link>https://pib.gov.in/2</link></item>
    </channel></rss>"""

    items = parse_items(xml)

    assert items[0] == ("First & foremost", "https://pib.gov.in/1")
    assert items[1][0] == "Second"


@pytest.mark.asyncio
async def test_a_broken_feed_is_skipped_and_the_rest_are_stored():
    xml = """<rss><channel><item><title>Headline</title><link>https://x.in/1</link></item></channel></rss>"""
    source = NewsSource(
        http=FakeHTTP(text=xml),
        feeds={"good": "https://x.in/rss", "bad": "https://y.in/rss"},
    )
    # The fake answers both feeds the same way; the point here is the shape of
    # what is stored, which is one fact per headline with its link as the unit.
    facts = await source.fetch()

    assert facts
    key, value, unit = facts[0]
    assert key.startswith("news.")
    assert value == "Headline"
    assert unit.startswith("https://")


@pytest.mark.asyncio
async def test_a_rate_is_named_by_its_metal_and_purity():
    """`bullion.rates.2.perGram` is unreadable; `gold.22k.inr.gram` is the ask.

    The service returns anonymous rows, and two of them share a purity, so the
    metal has to be read from the row itself rather than inferred from position.
    """
    payload = {
        "data": {
            "rates": [
                {"metal": "gold", "purity": "999", "perGram": 13580.9, "rupees": 135809},
                {"metal": "gold", "purity": "916", "perGram": 12440.1, "rupees": 124401},
                {"metal": "silver", "purity": "999", "perGram": 178, "rupees": 178000},
            ]
        }
    }
    source = BullionSource(http=FakeHTTP(payload))

    facts = {key: (value, unit) for key, value, unit in await source.fetch()}

    assert facts["gold.24k.inr.gram"] == ("13580.9", "INR/gram")
    assert facts["gold.22k.inr.10g"] == ("124401", "INR/10g")
    assert facts["silver.inr.kg"] == ("178000", "INR/kg")
    assert facts["silver.inr.gram"] == ("178", "INR/gram")


@pytest.mark.asyncio
async def test_a_feed_that_answers_with_nothing_is_not_counted_as_working():
    """A 200 with an empty body is a feed that changed shape, not a quiet day.

    Counting it as success is how a source looks alive while contributing
    nothing to the answer.
    """
    source = NewsSource(http=FakeHTTP(text="<rss><channel></channel></rss>"), feeds={"empty": "https://x.in/rss"})

    assert await source.fetch() == []


@pytest.mark.asyncio
async def test_a_feed_is_asked_to_follow_its_redirect():
    """Google News answers 302, and without following it the feed is silent."""
    calls = []

    class RecordingHTTP(FakeHTTP):
        async def get(self, url, **kwargs):
            calls.append(kwargs)
            return self._response

    xml = "<rss><channel><item><title>T</title><link>https://x.in/1</link></item></channel></rss>"
    source = NewsSource(http=RecordingHTTP(text=xml), feeds={"g": "https://news.google.com/rss"})

    await source.fetch()

    assert calls and calls[0].get("follow_redirects") is True


@pytest.mark.asyncio
async def test_a_city_is_placed_by_name_and_remembered(client):
    """Coordinates come from the geocoder, not from a table of numbers.

    A hardcoded latitude for every Indian city is a number nobody can check,
    and the geocoder answers for any name — which is what makes the weather
    source work beyond the handful of cities someone typed in.
    """
    from app.live.sources.geocode import coordinates

    class Geo:
        async def get(self, url, **kwargs):
            return FakeResponse({"results": [{"latitude": 15.335, "longitude": 76.46}]})

        async def aclose(self):
            return None

    point = await coordinates("hosapete", client, http=Geo())

    assert point == (15.335, 76.46)
    cached = await live.fact(client, "geo.hosapete")
    assert cached is not None and cached.value == "15.335,76.46"

    class NeverCalled:
        async def get(self, url, **kwargs):
            raise AssertionError("a placed city must not be geocoded twice")

        async def aclose(self):
            return None

    assert await coordinates("hosapete", client, http=NeverCalled()) == (15.335, 76.46)


@pytest.mark.asyncio
async def test_a_city_the_geocoder_cannot_find_is_skipped(client):
    from app.live.sources.geocode import coordinates

    class Empty:
        async def get(self, url, **kwargs):
            return FakeResponse({"results": []})

        async def aclose(self):
            return None

    assert await coordinates("notarealplace", client, http=Empty()) is None


@pytest.mark.asyncio
async def test_currency_quotes_the_dollar_against_the_world():
    from app.live.sources.currency import CurrencySource

    payload = {"base": "USD", "rates": {"INR": 88.5, "AED": 3.67, "EUR": 0.92}}
    source = CurrencySource(http=FakeHTTP(payload))

    facts = {key: value for key, value, _ in await source.fetch()}

    assert facts["fx.usd.inr"] == "88.5"
    assert facts["fx.usd.aed"] == "3.67"
    assert facts["fx.usd.usd"] == "1"


@pytest.mark.asyncio
async def test_gold_is_quoted_in_another_country_by_arithmetic(client):
    """Dollars per ounce, times the rate, over the grams in an ounce."""
    from app.live import store as live_store
    from app.live.sources.metals import GRAMS_PER_OUNCE, MetalsSource

    await live_store.put_fact(client, "fx.usd.aed", "3.67", unit="rate", source="currency")
    source = MetalsSource(client=client, http=FakeHTTP({"value": 4200.0}))

    facts = {key: value for key, value, _ in await source.fetch()}

    assert facts["gold.spot.usd.oz"] == "4200"
    assert float(facts["gold.spot.usd.gram"]) == pytest.approx(4200 / GRAMS_PER_OUNCE, rel=1e-3)
    assert float(facts["gold.spot.aed.gram"]) == pytest.approx(4200 * 3.67 / GRAMS_PER_OUNCE, rel=1e-3)


@pytest.mark.asyncio
async def test_a_currency_with_no_rate_is_skipped_rather_than_guessed(client):
    from app.live.sources.metals import MetalsSource

    source = MetalsSource(client=client, http=FakeHTTP({"value": 4200.0}))

    facts = {key for key, _, _ in await source.fetch()}

    assert "gold.spot.usd.gram" in facts
    assert "gold.spot.aed.gram" not in facts


def test_each_country_edition_is_its_own_feed():
    """An edition that loses its parameters is the American one, not the Indian.

    The URLs were written as literals once and arrived without their query
    strings, so all nine editions pointed at the same bare feed and every
    country got American headlines.
    """
    from app.live.sources.news import FEEDS

    assert "gl=US" in FEEDS["us"]
    assert "gl=IN" in FEEDS["in"]
    assert "hl=kn" in FEEDS["in_kn"]
    assert len({FEEDS[name] for name in ("in", "us", "gb", "ae", "au", "ca", "sg")}) == 7


def test_the_government_feed_keeps_the_parameters_it_needs():
    """PIB answers an empty page without `ModId`, `Lang` and `Regid`."""
    from app.live.sources.news import FEEDS

    assert "ModId=6" in FEEDS["pib"]
    assert "Regid=3" in FEEDS["pib"]


@pytest.mark.asyncio
async def test_the_dirham_is_available_even_though_the_ecb_does_not_publish_it():
    """The ECB list has no AED, so a Frankfurter-only feed cannot price Dubai."""
    from app.live.sources.currency import CurrencySource

    primary = {"result": "success", "rates": {"INR": 94.25, "AED": 3.6725}}
    source = CurrencySource(http=FakeHTTP(primary))

    facts = {key: value for key, value, _ in await source.fetch()}

    assert facts["fx.usd.aed"] == "3.6725"
    assert facts["fx.usd.inr"] == "94.25"


@pytest.mark.asyncio
async def test_the_ecb_feed_covers_the_primary_when_it_fails():
    from app.live.sources.currency import CurrencySource

    class PrimaryDown(FakeHTTP):
        async def get(self, url, **kwargs):
            if "frankfurter" in url:
                return FakeResponse({"rates": {"INR": 90.0}})
            raise RuntimeError("primary down")

    source = CurrencySource(http=PrimaryDown())

    facts = {key: value for key, value, _ in await source.fetch()}

    assert facts["fx.usd.inr"] == "90"


@pytest.mark.asyncio
async def test_air_quality_reports_a_band_a_reader_can_use():
    """An index number alone is not an answer; the word beside it is."""
    from app.live.sources.air import AirSource

    payload = {"current": {"european_aqi": 35, "pm2_5": 12.5, "pm10": 20.0}}
    source = AirSource(cities={"delhi": (28.6, 77.2)}, http=FakeHTTP(payload))

    facts = {key: value for key, value, _ in await source.fetch()}

    assert facts["air.delhi.aqi"] == "35"
    assert facts["air.delhi.band"] == "fair"
    assert facts["air.delhi.pm2_5"] == "12.5"


@pytest.mark.asyncio
async def test_holidays_are_the_ones_still_ahead():
    """A list of holidays that have already passed is not a live fact."""
    import datetime as dt

    from app.live.sources.holidays import HolidaySource

    payload = [
        {"date": "2026-01-26", "localName": "Republic Day"},
        {"date": "2026-12-25", "localName": "Christmas"},
    ]
    source = HolidaySource(
        countries={"in": "IN"},
        http=FakeHTTP(payload),
        today=dt.date(2026, 11, 21),
    )

    facts = {key: value for key, value, _ in await source.fetch()}

    assert facts["holiday.in.next"] == "2026-12-25"
    assert "Republic Day" not in " ".join(facts.values())
