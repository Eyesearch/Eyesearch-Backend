"""Fill the live facts from every source, and record which sources are alive.

One pass, one loop. A source that raises is recorded as a failure and the pass
carries on: a dead feed must not stop the prices, and a dead price source must
not stop the news.

This agent never touches the index. It writes `live_facts` and `live_sources`
and nothing else, which is what lets a live failure be fixed by opening one
folder instead of reading the crawler.
"""

from __future__ import annotations

import logging

from app.live.registry import record_fail, record_ok
from app.live.sources import SOURCES
from app.live.sources.geocode import coordinates
from app.live.sources.weather import CITIES as WEATHER_CITIES, WeatherSource
from app.live.store import expire, put_fact

logger = logging.getLogger(__name__)


class LiveFeedAgent:
    """Run every live source once and store what comes back."""

    def __init__(self, client, *, sources=None, settings=None) -> None:
        self._client = client
        self._settings = settings
        self._sources = sources
        self._city_cache: dict[str, tuple[float, float]] = {}

    def _build_sources(self):
        """Every source, with the weather cities resolved first.

        Built at run time, not at construction: the weather source cannot ask
        about a city it has no coordinates for, and the coordinates are only
        known once `_resolve_cities` has run. Building this in `__init__` handed
        the source an empty map, so the pass ran, reported success, and fetched
        no weather at all.
        """
        built = []
        for cls in SOURCES:
            if cls.__name__ == "WeatherSource":
                continue
            built.append(cls())
        built.insert(1, WeatherSource(cities=self._city_cache))
        return built

    async def _resolve_cities(self) -> None:
        """Fill `self._city_cache` before the pass, geocoding only what is new."""
        resolved: dict[str, tuple[float, float]] = {}
        for city in WEATHER_CITIES:
            point = await coordinates(city, self._client)
            if point is not None:
                resolved[city] = point
        self._city_cache = resolved
        if resolved:
            logger.info("live feed: %d cities placed", len(resolved))

    async def run(self) -> dict[str, int]:
        stored = 0
        failed = 0
        await self._resolve_cities()
        sources = self._sources if self._sources is not None else self._build_sources()
        for source in sources:
            try:
                facts = await source.fetch()
            except Exception as error:  # noqa: BLE001 - one source must not stop the pass
                failed += 1
                await record_fail(
                    self._client, source.name, str(error), getattr(source, "url", "")
                )
                continue

            if not facts:
                failed += 1
                await record_fail(
                    self._client, source.name, "no facts returned", getattr(source, "url", "")
                )
                continue

            written = 0
            for key, value, unit in facts:
                try:
                    await put_fact(
                        self._client,
                        key,
                        value,
                        unit=unit,
                        source=source.name,
                        ttl_seconds=getattr(source, "ttl_seconds", 900),
                    )
                    written += 1
                except Exception as error:  # noqa: BLE001 - one bad fact, not one bad pass
                    # A single unwritable row must not take the rest of the pass
                    # with it: the sources after this one would never run, and
                    # the log would say nothing about why.
                    logger.warning("live fact %s could not be stored: %s", key, error)
            stored += written
            if written:
                await record_ok(self._client, source.name, getattr(source, "url", ""))
            else:
                failed += 1
                await record_fail(
                    self._client, source.name, "nothing stored", getattr(source, "url", "")
                )

        # The ceiling comes from settings, not from the module constant: a
        # configured limit that the code ignores is worse than no limit, because
        # it reads as one.
        expired = await expire(self._client, getattr(self._settings, "live_max_facts", None))
        if stored:
            logger.info("live feed: stored %d facts, %d sources failed", stored, failed)
        return {"stored": stored, "failed": failed, "expired": expired}


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails when
# a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

#: The ceiling on how stale a stored fact may be before it is deleted. A price
#: kept for a day is not a price, it is a mistake with a timestamp on it.
MAX_FACT_TTL_SECONDS = 7 * 24 * 3600

CONTRACT = {
    "agent": "31_live_feed_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule(
            "MAX_FACT_TTL_SECONDS",
            globals().get("MAX_FACT_TTL_SECONDS"),
            _contract_rules.at_least(60),
            "MAX_FACT_TTL_SECONDS must satisfy at_least(60)",
        ),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
