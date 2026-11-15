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

#: The cities filled on every pass — the state capitals, the large cities of
#: every state, and the ones people actually ask about. Names, not coordinates:
#: a hardcoded latitude is a number nobody can check, and `geocode.coordinates`
#: resolves each one once and keeps it for a year.
CITIES: tuple[str, ...] = (
    # metros and union territory capitals
    "delhi", "mumbai", "kolkata", "chennai", "bengaluru", "hyderabad",
    "ahmedabad", "pune", "surat", "jaipur", "lucknow", "kanpur", "nagpur",
    "indore", "thane", "bhopal", "visakhapatnam", "patna", "vadodara",
    "ghaziabad", "ludhiana", "agra", "nashik", "faridabad", "meerut",
    "rajkot", "varanasi", "srinagar", "aurangabad", "dhanbad", "amritsar",
    "allahabad", "prayagraj", "ranchi", "howrah", "coimbatore", "jabalpur",
    "gwalior", "vijayawada", "jodhpur", "madurai", "raipur", "kota",
    "chandigarh", "guwahati", "solapur", "hubballi", "tiruchirappalli",
    "bareilly", "mysuru", "tiruppur", "gurugram", "aligarh", "jalandhar",
    "bhubaneswar", "salem", "warangal", "guntur", "bhiwandi", "saharanpur",
    "gorakhpur", "bikaner", "amravati", "noida", "jamshedpur", "bhilai",
    "cuttack", "firozabad", "kochi", "nellore", "bhavnagar", "dehradun",
    "durgapur", "asansol", "rourkela", "nanded", "kolhapur", "ajmer",
    "akola", "gulbarga", "kalaburagi", "jamnagar", "ujjain", "loni",
    "siliguri", "jhansi", "ulhasnagar", "jammu", "sangli", "mangaluru",
    "erode", "belagavi", "ambattur", "tirunelveli", "malegaon", "gaya",
    "thiruvananthapuram", "udaipur", "kakinada", "davanagere", "kozhikode",
    "kurnool", "rajahmundry", "bokaro", "south dumdum", "bellary",
    "patiala", "gopalpur", "agartala", "bhagalpur", "muzaffarnagar",
    "bhatinda", "puducherry", "shimla", "imphal", "shillong", "aizawl",
    "kohima", "gangtok", "itanagar", "panaji", "dispur", "port blair",
    "silchar", "haridwar", "nainital", "darjeeling", "ooty", "vijayapura",
    "hospet", "shivamogga", "tumakuru", "chikkamagaluru", "udupi",
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
