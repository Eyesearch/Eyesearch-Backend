"""The three live sources, against fixed responses.

No network in the suite: each source is handed a fake HTTP client and the test
asserts what it makes of a response the real service is documented to send.
"""

from __future__ import annotations

import pytest

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
