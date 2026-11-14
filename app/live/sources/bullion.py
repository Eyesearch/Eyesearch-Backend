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


#: Karat for a purity figure. IBJA quotes gold by purity, jewellers quote it by
#: karat, and a reader asking for "22 carat gold rate" should not have to know
#: that 916 is what that means.
KARAT_FOR_PURITY: dict[str, str] = {
    "999": "24k",
    "995": "24k",
    "916": "22k",
    "750": "18k",
    "585": "14k",
}


def _friendly(facts: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Named keys for each rate, built from the metal and purity beside it.

    The service returns an array of anonymous rows. `bullion.rates.2.perGram` is
    unreadable and indistinguishable from the row next to it; `gold.22k.inr.gram`
    is what a caller will actually ask for.
    """
    rows: dict[str, dict[str, str]] = {}
    for key, value, _unit in facts:
        parts = key.split(".")
        if len(parts) < 4 or parts[0] != NAME or parts[1] != "rates":
            continue
        rows.setdefault(parts[2], {})[parts[3]] = value

    named: list[tuple[str, str, str]] = []
    for index, fields in rows.items():
        metal = (fields.get("metal") or "").strip().lower()
        purity = (fields.get("purity") or "").strip()
        if not metal:
            continue
        # Silver is quoted by the kilogram, everything else by the gram.
        if metal == "silver":
            if fields.get("rupees"):
                named.append((f"silver.inr.kg", fields["rupees"], "INR/kg"))
            if fields.get("perGram"):
                named.append(("silver.inr.gram", fields["perGram"], "INR/gram"))
            continue
        label = KARAT_FOR_PURITY.get(purity, purity or "na")
        if fields.get("perGram"):
            named.append((f"{metal}.{label}.inr.gram", fields["perGram"], "INR/gram"))
        if fields.get("rupees"):
            named.append((f"{metal}.{label}.inr.10g", fields["rupees"], "INR/10g"))
    return named


def _unit_for(path: str) -> str:
    """What a number in this position is measured in.

    IBJA quotes gold twice — `rupees` is the price of ten grams, `perGram` the
    price of one — so the field name is the only thing that says which. Reading
    a per-10g figure as a per-gram one is a tenfold error, which is the kind of
    mistake that gets quoted back at you.
    """
    lowered = path.lower()
    if lowered.endswith("pergram"):
        return "INR/gram"
    if lowered.endswith("rupees"):
        return "INR/10g"
    if lowered.endswith("change"):
        return "INR"
    return ""


def _walk(node: Any, path: str = "") -> list[tuple[str, str]]:
    """Every leaf, keyed by its JSON path, as text.

    Strings are kept as well as numbers: the metal a rate belongs to is a word
    (`gold`, `silver`), and dropping it left six anonymous rows whose only
    distinguishing mark was a purity that two of them shared.
    """
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            found.extend(_walk(value, child))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_walk(value, f"{path}.{index}"))
    elif isinstance(node, bool):
        pass
    elif isinstance(node, (int, float)):
        found.append((path, f"{float(node):g}"))
    elif isinstance(node, str):
        text = node.strip()
        if text:
            found.append((path, text))
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
            facts.append((path, value, _unit_for(path)))
        facts.extend(_friendly(facts))
        for alias, path in ALIASES.items():
            for key, value, unit in list(facts):
                if key == path:
                    facts.append((alias, value, unit))
        return facts
