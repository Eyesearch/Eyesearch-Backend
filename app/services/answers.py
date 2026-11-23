"""How an index answer is shaped and rendered.

This was `app/live/compose.py`, which also chose between a live value and a
page. The live layer is gone; what remains is the part that describes a page
answer honestly — `answer: null`, the title and url as a source to cite, and
the markdown a model can read without parsing fields it was never told about.
"""

from __future__ import annotations

from typing import Any


def from_index(
    results: list[dict[str, Any]],
    query: str,
    *,
    official: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The page-index answer: what we hold, and that it is a page, not a value.

    `official` is the domain that owns a scheme or service the question named.
    When the index holds nothing, that is still worth saying: "we cannot read
    pmkisan.gov.in" is a different answer from "we have never heard of it", and
    a caller can act on the first.
    """
    if not results:
        payload: dict[str, Any] = {
            "query": query,
            "kind": "index",
            "answer": None,
            "freshness": "missing",
            "note": "No live source covers this question and the index holds nothing for it.",
        }
        if official:
            payload["official"] = official
            payload["note"] = (
                f"{official.get('name', 'This')} is published at "
                f"{official.get('domain', 'its official site')}, which we cannot read "
                "at the moment. The answer is not that it does not exist."
            )
        return payload
    top = results[0]
    return {
        "query": query,
        "kind": "index",
        # Deliberately null. A page title is not an answer: "The Nilgiris
        # District, Tamilnadu" filled this field once and a caller reading
        # `answer` had no way to know it was a page rather than a fact. The
        # title is still here, under `title`, next to its `url`.
        "answer": None,
        "title": top.get("title") or "",
        "url": top.get("url"),
        "snippet": top.get("snippet", ""),
        "freshness": "indexed",
        "source": top.get("url", ""),
        "note": (
            "This is a page we crawled, not a value we hold. Read `title`, `url` "
            "and `snippet` as a source to cite, not as an answer to repeat."
        ),
        "alternatives": [
            {"title": item.get("title"), "url": item.get("url")}
            for item in results[1:4]
        ],
    }


def as_markdown(payload: dict[str, Any]) -> str:
    """The same answer as markdown, for a caller that asked for prose.

    The age is in the text on purpose. A model reading this must be able to say
    when the value was true without having to parse a field it was never told
    about.
    """
    answer = payload.get("answer")
    if not answer:
        # An index answer is a source, so it is rendered as one: the reader is
        # told what we hold and where it is, not given a sentence to repeat.
        if payload.get("freshness") == "indexed" and payload.get("title"):
            lines = [
                f"**{payload['title']}**",
                "",
                f"- Source: {payload.get('url', '')}",
                "- This is a page we crawled, not a value we hold.",
            ]
            for alternative in payload.get("alternatives") or []:
                if alternative.get("title"):
                    lines.append(f"- Also held: {alternative['title']} ({alternative.get('url', '')})")
            return "\n".join(lines)
        return f"No live value is held for this question ({payload.get('freshness', 'missing')})."

    lines = [f"**{answer}**", ""]
    if payload.get("as_of"):
        lines.append(f"- As of: {payload['as_of']}")
    if payload.get("source"):
        lines.append(f"- Source: {payload['source']}")
    lines.append(f"- Freshness: {payload.get('freshness', 'unknown')}")

    answers = payload.get("answers")
    if isinstance(answers, list) and len(answers) > 1:
        lines.append("")
        lines.append("Also current:")
        for entry in answers[1:]:
            lines.append(f"- {entry.get('answer')} ({entry.get('as_of')})")
    return "\n".join(lines)
