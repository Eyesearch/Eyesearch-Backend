"""Which kind of question is this, and which live fact answers it.

A caller does not know that gold lives at `gold.22k.inr.gram`; it knows to ask
"what is the gold rate today". This module is the step between the two: it reads
the question, decides which source could answer it, and works out the exact key.

When nothing matches, the answer is `index` — not an error. The page index can
answer most questions; the router's job is to notice the ones where a live value
is the honest answer and a crawled page is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Cities the weather source fills. A question naming one of these is answered
#: live; one naming somewhere else is not guessed at.
from app.live.sources.weather import CITIES as FILLED_CITIES

#: The cities the weather source fills, plus the spellings people use for them.
#: Read from the source rather than repeated, so adding a city to the feed adds
#: it to the router in the same edit.
WEATHER_CITIES: tuple[str, ...] = FILLED_CITIES + (
    "bangalore", "mysore", "new delhi", "bombay", "madras", "calcutta",
    "trivandrum", "cochin", "benares", "allahabad", "gurgaon", "noida",
)

CITY_ALIASES: dict[str, str] = {
    "bangalore": "bengaluru",
    "mysore": "mysuru",
    "new delhi": "delhi",
    "bombay": "mumbai",
    "madras": "chennai",
    "calcutta": "kolkata",
    "trivandrum": "thiruvananthapuram",
    "cochin": "kochi",
    "benares": "varanasi",
    "gurgaon": "gurugram",
    "shimoga": "shivamogga",
    "hubli": "hubballi",
    "mangalore": "mangaluru",
    "belgaum": "belagavi",
    "gulbarga": "kalaburagi",
    "tumkur": "tumakuru",
    "chickmagalur": "chikkamagaluru",
    "udipi": "udupi",
    "pondicherry": "puducherry",
    "simla": "shimla",
    "baroda": "vadodara",
    "poona": "pune",
    "calicut": "kozhikode",
    "tellicherry": "kannur",
    "quilon": "kollam",
    "tanjore": "thanjavur",
    "trichy": "tiruchirappalli",
    "vizag": "visakhapatnam",
    "vijaywada": "vijayawada",
    "jubbulpore": "jabalpur",
}

#: Words that make a question about a live value rather than a page.
BULLION_WORDS = ("gold", "silver", "bullion", "ಚಿನ್ನ", "ಬೆಂಗಳೂರು", "ಬೆಳ್ಳಿ", "bangar")
WEATHER_WORDS = ("weather", "temperature", "forecast", "rain", "ಹವಾಮಾನ", "ಮಳೆ", "mausam")
NEWS_WORDS = ("news", "headline", "ಸುದ್ದಿ", "breaking", "headlines")

#: Country editions a news question can name. The key is what the reader might
#: type, the value is the feed it means.
NEWS_EDITIONS: dict[str, str] = {
    "us": "us", "usa": "us", "america": "us", "united states": "us",
    "uk": "gb", "britain": "gb", "england": "gb", "london": "gb",
    "uae": "ae", "dubai": "ae", "emirates": "ae",
    "australia": "au", "canada": "ca", "singapore": "sg",
    "india": "in", "karnataka": "in", "kerala": "in", "tamil nadu": "in",
    "kannada": "in_kn", "hindi": "in_hi",
}

#: Currencies a gold question can name, and the fact each one means.
GOLD_CURRENCIES: dict[str, str] = {
    # currencies, including the plurals people actually type
    "dollar": "usd", "dollars": "usd", "usd": "usd", "us": "usd",
    "euro": "eur", "euros": "eur", "eur": "eur",
    "pound": "gbp", "pounds": "gbp", "gbp": "gbp", "sterling": "gbp",
    "dirham": "aed", "dirhams": "aed", "aed": "aed",
    "riyal": "sar", "riyals": "sar", "sar": "sar",
    "singapore dollar": "sgd", "sgd": "sgd",
    "australian dollar": "aud", "aud": "aud",
    "canadian dollar": "cad", "cad": "cad",
    "yen": "jpy", "jpy": "jpy",
    # places, because "gold price in dubai" is how the question is asked
    "dubai": "aed", "abu dhabi": "aed", "sharjah": "aed",
    "london": "gbp", "britain": "gbp", "england": "gbp", "uk": "gbp",
    "new york": "usd", "america": "usd", "usa": "usd",
    "paris": "eur", "germany": "eur", "france": "eur", "italy": "eur",
    "tokyo": "jpy", "japan": "jpy",
    "singapore": "sgd", "australia": "aud", "canada": "cad",
    "saudi": "sar", "riyadh": "sar",
    # India keeps the IBJA rate, so its words route back to the retail quote
    "rupee": "inr", "rupees": "inr", "inr": "inr", "india": "inr",
}


@dataclass
class Route:
    """Where a question should be answered from."""

    kind: str
    key: str | None = None
    city: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "key": self.key or "", "reason": self.reason}


def _karat(query: str) -> str:
    if re.search(r"\b24\s*k|\b999\b", query, re.IGNORECASE):
        return "24k"
    if re.search(r"\b18\s*k|\b750\b", query, re.IGNORECASE):
        return "18k"
    if re.search(r"\b14\s*k|\b585\b", query, re.IGNORECASE):
        return "14k"
    return "22k"


def _city(query: str) -> str | None:
    """The city a question names, resolved to the name the feed stores it under.

    The alias keys are searched as well as the city names: "shimoga" is how
    people write Shivamogga, and a router that only knew the official spelling
    sent the question to the index.
    """
    lowered = query.lower()
    for city in WEATHER_CITIES + tuple(CITY_ALIASES):
        if re.search(rf"\b{re.escape(city)}\b", lowered):
            return CITY_ALIASES.get(city, city)
    return None


def route(query: str) -> Route:
    """The kind of answer this question wants, and the key when one is known."""
    text = (query or "").strip()
    lowered = text.lower()

    if any(word in lowered for word in BULLION_WORDS):
        if "silver" in lowered or "ಬೆಳ್ಳಿ" in text:
            if re.search(r"\bkg\b|kilo", lowered):
                return Route("bullion", "silver.inr.kg", reason="silver by the kilogram")
            return Route("bullion", "silver.inr.gram", reason="silver by the gram")
        # A question that names a currency or a country other than India wants
        # the spot price in that currency. IBJA quotes the Indian retail rate and
        # nothing else, so answering "gold price in dubai" with it would be a
        # rupee figure under a dirham question.
        for word, code in GOLD_CURRENCIES.items():
            if re.search(rf"\b{re.escape(word)}\b", lowered):
                if code == "inr":
                    break
                return Route("bullion", f"gold.spot.{code}.gram", reason=f"spot gold in {code}")
        karat = _karat(lowered)
        if re.search(r"\b10\s*(g|gm|gram)\b", lowered):
            return Route("bullion", f"gold.{karat}.inr.10g", reason=f"{karat} gold per ten grams")
        return Route("bullion", f"gold.{karat}.inr.gram", reason=f"{karat} gold per gram")

    if any(word in lowered for word in WEATHER_WORDS):
        city = _city(text)
        if city:
            return Route("weather", f"weather.{city}.temperature", city=city, reason=f"weather in {city}")
        # A weather question with no city we fill is answered from the index
        # rather than with the temperature of somewhere the reader never asked
        # about.
        return Route("index", reason="weather question without a city we fill")

    if any(word in lowered for word in NEWS_WORDS):
        for word, edition in NEWS_EDITIONS.items():
            if re.search(rf"\b{re.escape(word)}\b", lowered):
                return Route("news", edition, reason=f"{edition} headlines")
        return Route("news", reason="headlines")

    return Route("index", reason="no live source for this")
