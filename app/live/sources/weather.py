"""Current weather from Open-Meteo.

No API key, no registration, and the response shape is documented and stable.
One call per city rather than one per reader: the feed agent fills these on a
timer and the API answers from the table.
"""

from __future__ import annotations

import httpx

NAME = "weather"
URL = "https://api.open-meteo.com/v1/forecast"
TTL_SECONDS = 900
TIMEOUT_SECONDS = 10.0

#: The cities filled on every pass. Latitude and longitude are fixed here rather
#: than geocoded per request: a geocode call per reader would be slower than the
#: weather call it feeds.
CITIES: dict[str, tuple[float, float]] = {
    "bengaluru": (12.9716, 77.5946),
    "mysuru": (12.2958, 76.6394),
    "mumbai": (19.0760, 72.8777),
    "delhi": (28.6139, 77.2090),
    "chennai": (13.0827, 80.2707),
    "hyderabad": (17.3850, 78.4867),
    "kolkata": (22.5726, 88.3639),
    "pune": (18.5204, 73.8567),
}

#: WMO weather codes, condensed to the words a reader wants.
WMO_CODES: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "rain showers",
    81: "rain showers",
    82: "violent rain showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def describe(code: int | None) -> str:
    return WMO_CODES.get(int(code or 0), "unknown")


class WeatherSource:
    name = NAME
    url = URL
    ttl_seconds = TTL_SECONDS

    def __init__(self, http: httpx.AsyncClient | None = None, cities: dict | None = None) -> None:
        self._http = http
        self._cities = cities or CITIES

    async def fetch(self) -> list[tuple[str, str, str]]:
        facts: list[tuple[str, str, str]] = []
        client = self._http or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            for city, (lat, lon) in self._cities.items():
                params = {
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,precipitation,weather_code",
                }
                try:
                    response = await client.get(URL, params=params)
                    response.raise_for_status()
                    current = response.json().get("current", {})
                except Exception:
                    # One city failing must not lose the others.
                    continue

                if "temperature_2m" in current:
                    facts.append(
                        (f"weather.{city}.temperature", f"{current['temperature_2m']:g}", "°C")
                    )
                if "relative_humidity_2m" in current:
                    facts.append(
                        (f"weather.{city}.humidity", f"{current['relative_humidity_2m']:g}", "%")
                    )
                if "precipitation" in current:
                    facts.append(
                        (f"weather.{city}.precipitation", f"{current['precipitation']:g}", "mm")
                    )
                if "weather_code" in current:
                    facts.append(
                        (
                            f"weather.{city}.condition",
                            describe(current["weather_code"]),
                            "",
                        )
                    )
        finally:
            if self._http is None:
                await client.aclose()
        return facts
