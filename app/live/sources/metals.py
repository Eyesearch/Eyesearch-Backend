"""Gold in dollars, and gold in whatever currency the reader asked in.

IBJA quotes the Indian retail rate and says nothing about anywhere else. The
spot price is quoted in dollars per troy ounce, so every other country's answer
is arithmetic: dollars per ounce, times the day's rate, over 31.1035 grams.

Two things are deliberately kept apart. India keeps the IBJA rate, because that
is the number a buyer there actually sees, and it is not the spot price. Every
other currency is labelled `spot`, because it is.
"""

from __future__ import annotations

import logging

import httpx

from app.live.store import fact

logger = logging.getLogger(__name__)

NAME = "metals"
URL = "https://snapdata.dev/api/v1/gold/world/latest.json"
TTL_SECONDS = 1800
TIMEOUT_SECONDS = 10.0

#: Grams in a troy ounce. The unit every spot quote is given in.
GRAMS_PER_OUNCE = 31.1035

#: Currencies gold is quoted in, alongside the IBJA rupee rate.
QUOTED: tuple[str, ...] = (
    "usd", "eur", "gbp", "aed", "sar", "qar", "sgd", "aud", "cad", "jpy",
)


def _spot_usd_ounce(payload: object) -> float | None:
    """The dollar price of an ounce, from whichever shape came back."""
    if isinstance(payload, dict):
        for key in ("value", "price", "close", "latest", "rate"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value.replace(",", ""))
                except ValueError:
                    pass
        data = payload.get("data")
        if data is not None:
            return _spot_usd_ounce(data)
        for value in payload.values():
            found = _spot_usd_ounce(value)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for entry in payload:
            found = _spot_usd_ounce(entry)
            if found is not None:
                return found
    elif isinstance(payload, (int, float)) and not isinstance(payload, bool):
        return float(payload)
    return None


class MetalsSource:
    name = NAME
    url = URL
    ttl_seconds = TTL_SECONDS

    def __init__(self, client=None, http: httpx.AsyncClient | None = None) -> None:
        #: The database handle, used to read the exchange rates the currency
        #: source stored. Gold in dirhams is not a gold question, it is a gold
        #: question and a currency question.
        self._client = client
        self._http = http

    async def _rate(self, code: str) -> float | None:
        if code == "usd" or self._client is None:
            return 1.0 if code == "usd" else None
        item = await fact(self._client, f"fx.usd.{code}")
        if item is None:
            return None
        try:
            return float(item.value)
        except ValueError:
            return None

    async def fetch(self) -> list[tuple[str, str, str]]:
        client = self._http or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            response = await client.get(URL, follow_redirects=True)
            response.raise_for_status()
            payload = response.json()
        finally:
            if self._http is None:
                await client.aclose()

        ounce_usd = _spot_usd_ounce(payload)
        if not ounce_usd:
            logger.warning("metals: no spot price in the response")
            return []

        facts: list[tuple[str, str, str]] = [
            ("gold.spot.usd.oz", f"{ounce_usd:g}", "USD/oz"),
            ("gold.spot.usd.gram", f"{ounce_usd / GRAMS_PER_OUNCE:.2f}", "USD/gram"),
        ]
        for code in QUOTED:
            rate = await self._rate(code)
            if rate is None:
                continue
            per_gram = ounce_usd * rate / GRAMS_PER_OUNCE
            facts.append((f"gold.spot.{code}.gram", f"{per_gram:.2f}", f"{code.upper()}/gram"))
        return facts
