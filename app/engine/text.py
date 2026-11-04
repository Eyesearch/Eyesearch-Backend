"""Text helpers shared by the index, the parser and the federated fleet.

One home for truncation, because there were two behaviours: the parser cut by
character index and produced "Rech" from "Recharge", while the courier cut at a
word boundary. Which one a reader saw depended on where the text came from.
"""

from __future__ import annotations

#: Characters that should not be left dangling at the end of a cut. A title
#: ending in "and" or a comma reads as broken rather than as abbreviated.
_TRAILING = " ,;:-—–|/&+"


def truncate_at_word(text: str, limit: int, *, ellipsis: bool = False) -> str:
    """Cut `text` at a word boundary, never mid-word.

    A hard slice at fifty characters turns "Recharge" into "Rech", which is not
    an abbreviation of anything — it is a different word. Cutting at the last
    space keeps the words that are shown intact, which is the only property a
    truncated label actually needs.

    `ellipsis` is off by default because a card renders the whole field: an
    ellipsis in a stored title would be re-stored on the next crawl and
    accumulate. It is for display, not for storage.
    """
    value = " ".join((text or "").split())
    if limit <= 0 or len(value) <= limit:
        return value

    window = value[:limit]
    cut = window.rsplit(" ", 1)[0].rstrip(_TRAILING) if " " in window else window
    # A single word longer than the limit has no boundary to cut at; keeping the
    # window is better than returning nothing.
    if not cut:
        cut = window.rstrip(_TRAILING)
    return f"{cut}…" if ellipsis else cut


__all__ = ["truncate_at_word"]
