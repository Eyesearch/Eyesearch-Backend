"""Current weather from Open-Meteo.

No API key, no registration, and the response shape is documented and stable.
One call per city rather than one per reader: the feed agent fills these on a
timer and the API answers from the table.
"""

from __future__ import annotations

import httpx

NAME = "weather"
URL = "https://api.open-meteo.com/v1/forecast"
TTL_SECONDS = 1800
TIMEOUT_SECONDS = 10.0

#: The cities filled on every pass. Names, not coordinates: a hardcoded
#: latitude is a number nobody can check, and `geocode.coordinates` resolves
#: each one once and keeps it for a year.
#:
#: The list is the world's large cities plus every Indian state capital, so a
#: reader anywhere gets their own weather without asking, and anyone else is
#: placed on demand from the name they used.
CITIES: tuple[str, ...] = (
    # India
    "delhi", "mumbai", "kolkata", "chennai", "bengaluru", "hyderabad",
    "ahmedabad", "pune", "jaipur", "lucknow", "kanpur", "nagpur", "indore",
    "bhopal", "patna", "visakhapatnam", "vadodara", "coimbatore", "kochi",
    "guwahati", "chandigarh", "bhubaneswar", "thiruvananthapuram", "ranchi",
    "raipur", "dehradun", "shimla", "srinagar", "jammu", "gangtok",
    "shillong", "imphal", "aizawl", "kohima", "agartala", "itanagar",
    "panaji", "mysuru", "mangaluru", "hubballi", "belagavi", "shivamogga",
    "vijayawada", "madurai", "tiruchirappalli", "salem", "warangal",
    # Asia
    "tokyo", "osaka", "seoul", "beijing", "shanghai", "hong kong",
    "singapore", "kuala lumpur", "jakarta", "bangkok", "manila", "hanoi",
    "dhaka", "colombo", "kathmandu", "karachi", "lahore", "islamabad",
    "dubai", "abu dhabi", "doha", "riyadh", "kuwait city", "muscat",
    "manama", "tehran", "baghdad", "tel aviv", "istanbul", "tashkent",
    # Europe
    "london", "paris", "berlin", "madrid", "rome", "amsterdam", "brussels",
    "vienna", "zurich", "stockholm", "oslo", "copenhagen", "helsinki",
    "dublin", "lisbon", "prague", "warsaw", "budapest", "athens", "moscow",
    "kyiv", "bucharest", "sofia", "belgrade", "zagreb",
    # Americas
    "new york", "los angeles", "chicago", "houston", "toronto", "vancouver",
    "montreal", "mexico city", "sao paulo", "rio de janeiro", "buenos aires",
    "bogota", "lima", "santiago", "san francisco", "seattle", "miami",
    "washington", "boston", "atlanta", "dallas",
    # Africa and Oceania
    "cairo", "lagos", "nairobi", "johannesburg", "cape town", "accra",
    "addis ababa", "casablanca", "dakar", "sydney", "melbourne", "brisbane",
    "perth", "auckland", "wellington",
)

#: How many cities go into one request. Open-Meteo accepts several coordinates
#: in one call, and 130 separate calls every fifteen minutes would spend the
#: free budget on the same weather.
CITIES_PER_CALL = 40

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

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        cities: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self._http = http
        #: `{city: (lat, lon)}`, resolved by the agent from the cache. Without it
        #: the source has nothing to ask about and fetches nothing.
        self._cities = cities or {}

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
