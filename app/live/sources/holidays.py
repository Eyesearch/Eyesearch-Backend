"""Public holidays, from Nager.Date.

No key, no registration, and one call per country per year. A holiday is the
easiest kind of live fact: it changes once a year and matters exactly when
someone is planning around it.
"""

from __future__ import annotations

import datetime as dt

import httpx

NAME = "holiday"
URL = "https://date.nager.at/api/v3/PublicHolidays"
#: A holiday does not change within the day; a daily refresh is generous.
TTL_SECONDS = 86400
TIMEOUT_SECONDS = 10.0

#: The countries filled on every pass. India first, then the places an Indian
#: reader is most likely to be travelling to or from.
COUNTRIES: dict[str, str] = {
    "in": "IN",
    "us": "US",
    "gb": "GB",
    "ae": "AE",
    "sg": "SG",
    "au": "AU",
    "ca": "CA",
}

#: How many upcoming holidays are kept per country.
UPCOMING = 8


class HolidaySource:
    """The next few public holidays for each configured country."""

    name = NAME
    ttl_seconds = TTL_SECONDS

    def __init__(self, countries: dict[str, str] | None = None, http=None, today=None) -> None:
        self._countries = dict(countries or COUNTRIES)
        self._http = http
        self._today = today

    async def fetch(self) -> list[tuple[str, str, str]]:
        today = self._today or dt.date.today()
        facts: list[tuple[str, str, str]] = []
        # The year rolls over in the middle of December; asking for next year as
        # well keeps the list useful without a second pass in January.
        years = (today.year, today.year + 1)

        async def one(client: httpx.AsyncClient) -> None:
            for name, code in self._countries.items():
                upcoming: list[tuple[dt.date, str]] = []
                for year in years:
                    try:
                        response = await client.get(f"{URL}/{year}/{code}")
                        response.raise_for_status()
                        entries = response.json() or []
                    except Exception:
                        # One country that does not answer is one country.
                        continue
                    for entry in entries:
                        try:
                            when = dt.date.fromisoformat(str(entry.get("date")))
                        except ValueError:
                            continue
                        if when >= today:
                            upcoming.append((when, str(entry.get("localName") or entry.get("name") or "")))
                upcoming.sort()
                for index, (when, label) in enumerate(upcoming[:UPCOMING], start=1):
                    if not label:
                        continue
                    facts.append((f"holiday.{name}.{index}", f"{when.isoformat()} {label}", "date"))
                if upcoming:
                    facts.append((f"holiday.{name}.next", upcoming[0][0].isoformat(), "date"))

        if self._http is not None:
            await one(self._http)
            return facts

        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            await one(client)
        return facts
