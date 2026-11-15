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
#: The primary feed. It covers about 160 currencies, including the Gulf ones —
#: which matters, because the ECB list Frankfurter serves does not contain the
#: dirham at all. "gold price in dubai" answered from Frankfurter alone could
#: never work: there was no rate to convert with.
URL = "https://open.er-api.com/v6/latest/USD"
#: The ECB feed, used when the primary is unreachable. Fewer currencies.
FALLBACK_URL = "https://api.frankfurter.app/latest"
TTL_SECONDS = 6 * 3600
TIMEOUT_SECONDS = 10.0

#: The currencies quoted. Frankfurter returns about thirty; these are the ones
#: a reader outside India is likely to ask in.
QUOTED: tuple[str, ...] = (
    "INR", "EUR", "GBP", "AED", "SAR", "QAR", "KWD", "OMR", "BHD", "SGD",
    "AUD", "CAD", "JPY", "CNY", "CHF", "MYR", "THB", "ZAR", "NZD", "HKD",
    "PKR", "BDT", "LKR", "NPR", "IDR", "PHP", "KRW", "BRL", "MXN", "TRY",
)


class CurrencySource:
    name = NAME
    url = URL
    ttl_seconds = TTL_SECONDS

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http

    async def _rates(self, client) -> dict:
        """Dollar rates from the primary feed, or the ECB one if it is down."""
        try:
            response = await client.get(URL, follow_redirects=True)
            response.raise_for_status()
            payload = response.json() or {}
            if payload.get("result", "success") == "success":
                rates = payload.get("rates") or {}
                if rates:
                    return rates
        except Exception as error:  # noqa: BLE001 - the fallback is the answer
            logger.warning("currency: primary feed failed: %s", error)

        try:
            response = await client.get(
                FALLBACK_URL, params={"from": "USD"}, follow_redirects=True
            )
            response.raise_for_status()
            return (response.json() or {}).get("rates") or {}
        except Exception as error:  # noqa: BLE001
            logger.warning("currency: fallback feed failed: %s", error)
            return {}

    async def fetch(self) -> list[tuple[str, str, str]]:
        """One fact per currency, all quoted against the dollar."""
        client = self._http or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            rates = await self._rates(client)
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
