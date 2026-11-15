"""Turning a city name into coordinates, once, and remembering it.

The weather source needs a latitude and longitude, and hardcoding them for every
Indian city would mean a table of numbers nobody can check. Open-Meteo's
geocoding endpoint resolves the name instead, and the answer is cached in
`live_facts` for a year: a city does not move, and geocoding it again on every
pass would spend the request budget the weather itself needs.
"""

from __future__ import annotations

import logging

import httpx

from app.live.store import fact, put_fact

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
TIMEOUT_SECONDS = 10.0

#: A year. Coordinates do not change.
GEO_TTL_SECONDS = 365 * 24 * 3600


async def coordinates(
    city: str,
    client,
    *,
    http: httpx.AsyncClient | None = None,
    country_code: str | None = None,
) -> tuple[float, float] | None:
    """`(latitude, longitude)` for a city anywhere, cached for a year.

    No country is assumed. The feed asks about cities in every country, and a
    lookup pinned to one of them would have placed "springfield" in the wrong
    continent without saying so.
    """
    key = f"geo.{city}"
    cached = await fact(client, key)
    if cached is not None and "," in cached.value:
        try:
            lat, lon = cached.value.split(",", 1)
            return float(lat), float(lon)
        except ValueError:
            pass

    client_http = http or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    try:
        params = {"name": city, "count": 1, "language": "en", "format": "json"}
        if country_code:
            params["countryCode"] = country_code
        response = await client_http.get(GEOCODE_URL, params=params, follow_redirects=True)
        response.raise_for_status()
        results = (response.json() or {}).get("results") or []
    except Exception as error:  # noqa: BLE001 - a city we cannot place is skipped
        logger.warning("geocode %s failed: %s", city, error)
        return None
    finally:
        if http is None:
            await client_http.aclose()

    if not results:
        logger.warning("geocode %s found nothing", city)
        return None

    first = results[0]
    lat, lon = float(first["latitude"]), float(first["longitude"])
    await put_fact(client, key, f"{lat},{lon}", unit="lat,lon", source="geocode", ttl_seconds=GEO_TTL_SECONDS)
    return lat, lon
