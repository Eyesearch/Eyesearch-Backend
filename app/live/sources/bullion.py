"""Gold, silver and platinum rates from IBJA, via api.dharun.dev.

No API key. IBJA publishes twice a working day, so a fifteen-minute poll is
already faster than the source changes.

The response shape is not published in the docs, so the adapter does not guess
at field names: it walks the JSON, turns every numeric leaf into a fact keyed by
its own path, and lets the reader ask for the path. After the first live fetch,
`GET /v1/live` lists exactly which keys the source produced, and a friendly
alias can be added here with the real names in front of us rather than assumed.
"""

from __future__ import annotations

from typing import Any

import httpx

NAME = "bullion"
URL = "https://api.dharun.dev/v1/bullion"
TTL_SECONDS = 900
TIMEOUT_SECONDS = 10.0

#: Friendly keys mapped to the paths the source actually uses. Filled in from a
#: real response; until then the raw paths are still queryable.
ALIASES: dict[str, str] = {}


def _walk(node: Any, path: str = "") -> list[tuple[str, float]]:
    """Every numeric leaf, keyed by its JSON path."""
    found: list[tuple[str, float]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            found.extend(_walk(value, child))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_walk(value, f"{path}.{index}"))
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        found.append((path, float(node)))
    elif isinstance(node, str):
        try:
            found.append((path, float(node.replace(",", "").strip())))
        except ValueError:
            pass
    return found


class BullionSource:
    name = NAME
    url = URL
    ttl_seconds = TTL_SECONDS

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http

    async def fetch(self) -> list[tuple[str, str, str]]:
        """`(key, value, unit)` for every rate the source returned."""
        client = self._http or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            response = await client.get(URL, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
        finally:
            if self._http is None:
                await client.aclose()

        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        facts: list[tuple[str, str, str]] = []
        for path, value in _walk(data, NAME):
            unit = "INR/10g" if "10" in path else "INR/gram"
            facts.append((path, f"{value:g}", unit))
        for alias, path in ALIASES.items():
            for key, value, unit in list(facts):
                if key == path:
                    facts.append((alias, value, unit))
        return facts
