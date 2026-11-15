"""Exchange rates from Frankfurter, the ECB's own feed.

No key, no registration, one request a day's worth of data. The rates are also
what the metals source needs to quote gold outside India: a spot price in
dollars is not an answer to "what is gold in dirhams".
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

NAME = "currency"
URL = "https://api.frankfurter.app/latest"
TTL_SECONDS = 6 * 3600
TIMEOUT_SECONDS = 10.0

#: The currencies quoted. Frankfurter returns about thirty; these are the ones
#: a reader outside India is likely to ask in.
QUOTED: tuple[str, ...] = (
    "INR", "EUR", "GBP", "AED", "SAR", "QAR", "SGD", "AUD", "CAD", "JPY",
    "CNY", "CHF", "MYR", "THB", "ZAR", "NZD", "HKD", "KWD", "OMR", "BHD",
)


class CurrencySource:
    name = NAME
    url = URL
    ttl_seconds = TTL_SECONDS

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http

    async def fetch(self) -> list[tuple[str, str, str]]:
        """One fact per currency, all quoted against the dollar."""
        client = self._http or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            response = await client.get(URL, params={"from": "USD"}, follow_redirects=True)
            response.raise_for_status()
            rates = (response.json() or {}).get("rates") or {}
        finally:
            if self._http is None:
                await client.aclose()

        if not rates:
            logger.warning("currency: no rates returned")
            return []

        facts: list[tuple[str, str, str]] = [("fx.usd.usd", "1", "rate")]
        for code in QUOTED:
            value = rates.get(code)
            if value:
                facts.append((f"fx.usd.{code.lower()}", f"{float(value):g}", "rate"))
        return facts
