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
WEATHER_CITIES: tuple[str, ...] = (
    "bengaluru", "bangalore", "mysuru", "mysore", "mumbai", "delhi",
    "new delhi", "chennai", "hyderabad", "kolkata", "pune",
)

CITY_ALIASES: dict[str, str] = {
    "bangalore": "bengaluru",
    "mysore": "mysuru",
    "new delhi": "delhi",
}

#: Words that make a question about a live value rather than a page.
BULLION_WORDS = ("gold", "silver", "bullion", "ಚಿನ್ನ", "ಬೆಂಗಳೂರು", "ಬೆಳ್ಳಿ", "bangar")
WEATHER_WORDS = ("weather", "temperature", "forecast", "rain", "ಹವಾಮಾನ", "ಮಳೆ", "mausam")
NEWS_WORDS = ("news", "headline", "ಸುದ್ದಿ", "breaking", "headlines")


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
    lowered = query.lower()
    for city in WEATHER_CITIES:
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
        return Route("news", reason="headlines")

    return Route("index", reason="no live source for this")
