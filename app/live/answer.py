"""The shape `/v1/live` returns.

Every answer says when it was true and where it came from. A caller that cannot
see the age of a price will present a stale one as today's, which is the whole
reason this layer exists.
"""

from __future__ import annotations

from typing import Any

from app.live.store import LiveFact


def fact_answer(item: LiveFact) -> dict[str, Any]:
    value = item.value
    if item.unit and not item.unit.startswith("http"):
        answer = f"{value} {item.unit}".strip()
    else:
        answer = value
    return {
        "key": item.key,
        "answer": answer,
        "value": value,
        "unit": item.unit,
        "as_of": item.fetched_at,
        "source": item.source,
        "freshness": "live" if item.fresh else "stale",
    }


def facts_answer(items: list[LiveFact]) -> dict[str, Any]:
    return {
        "count": len(items),
        "facts": [fact_answer(item) for item in items],
    }


def missing_answer(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "answer": None,
        "freshness": "missing",
        "note": (
            "No live value is held for this key. It may never have been fetched, "
            "or its time-to-live may have passed."
        ),
    }
