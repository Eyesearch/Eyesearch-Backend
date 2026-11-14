"""One answer from whichever layer can honestly give it.

The reader asked a question, not a question about our architecture. This turns a
routed question into a single response: a live value when a live source can
answer it, the index when it cannot, and always with the age and the origin on
the front so a caller can say "as of" instead of guessing.
"""

from __future__ import annotations

from typing import Any

from app.live.answer import fact_answer
from app.live.router import Route
from app.live.store import all_facts, fact

#: Headlines returned for a news question.
NEWS_LIMIT = 5


async def from_live(client, routed: Route) -> dict[str, Any] | None:
    """The live answer for a routed question, or None when there is not one."""
    if routed.kind == "news":
        items = await all_facts(client, prefix="news.", limit=NEWS_LIMIT)
        if not items:
            return None
        return {
            "kind": "news",
            "count": len(items),
            "answers": [fact_answer(item) for item in items],
            "freshness": "live" if any(item.fresh for item in items) else "stale",
            "as_of": items[0].fetched_at,
            "source": "news",
        }

    if not routed.key:
        return None

    item = await fact(client, routed.key)
    if item is None:
        return None

    answer = fact_answer(item)
    answer["kind"] = routed.kind
    return answer


def from_index(results: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """The page-index answer: what we hold, and that it is a page, not a value."""
    if not results:
        return {
            "query": query,
            "kind": "index",
            "answer": None,
            "freshness": "missing",
            "note": "No live source covers this question and the index holds nothing for it.",
        }
    top = results[0]
    return {
        "query": query,
        "kind": "index",
        "answer": top.get("title") or top.get("url"),
        "url": top.get("url"),
        "snippet": top.get("snippet", ""),
        "freshness": "indexed",
        "source": top.get("url", ""),
        "note": "This is a page we crawled, not a value we hold. Its age is the crawl time of that page.",
        "alternatives": [
            {"title": item.get("title"), "url": item.get("url")}
            for item in results[1:4]
        ],
    }
