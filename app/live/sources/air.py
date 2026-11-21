"""Air quality from Open-Meteo, for the same cities the weather covers.

No key, no registration, and the same geocoded coordinates the weather source
already holds — so a city that has a temperature has an air reading too, and
neither one costs a geocode call.
"""

from __future__ import annotations

import httpx

NAME = "air"
URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
TTL_SECONDS = 1800
TIMEOUT_SECONDS = 10.0

#: Open-Meteo's own banding, so a caller does not have to invent thresholds.
#: The European AQI runs 0-100 with these boundaries.
BANDS: tuple[tuple[int, str], ...] = (
    (20, "good"),
    (40, "fair"),
    (60, "moderate"),
    (80, "poor"),
    (100, "very poor"),
)

FIELDS = "pm2_5,pm10,european_aqi"


def band(aqi: float) -> str:
    """The word for an index value, in Open-Meteo's own scale."""
    for ceiling, label in BANDS:
        if aqi <= ceiling:
            return label
    return "extremely poor"


class AirSource:
    """Air quality for the cities the weather source was given."""

    name = NAME
    ttl_seconds = TTL_SECONDS

    def __init__(self, cities: dict[str, tuple[float, float]] | None = None, http=None) -> None:
        self._cities = dict(cities or {})
        self._http = http

    async def fetch(self) -> list[tuple[str, str, str]]:
        facts: list[tuple[str, str, str]] = []
        if not self._cities:
            return facts

        async def one(client: httpx.AsyncClient) -> None:
            for city, (latitude, longitude) in self._cities.items():
                try:
                    response = await client.get(
                        URL,
                        params={
                            "latitude": latitude,
                            "longitude": longitude,
                            "current": FIELDS,
                        },
                    )
                    response.raise_for_status()
                    current = (response.json() or {}).get("current") or {}
                except Exception:
                    # One city that does not answer is one city.
                    continue
                aqi = current.get("european_aqi")
                if aqi is not None:
                    facts.append((f"air.{city}.aqi", str(aqi), "EAQI"))
                    facts.append((f"air.{city}.band", band(float(aqi)), ""))
                if current.get("pm2_5") is not None:
                    facts.append((f"air.{city}.pm2_5", str(current["pm2_5"]), "µg/m³"))
                if current.get("pm10") is not None:
                    facts.append((f"air.{city}.pm10", str(current["pm10"]), "µg/m³"))

        if self._http is not None:
            await one(self._http)
            return facts

        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            await one(client)
        return facts
