"""Prefix completion from RAM.

`suggest()` already answers completions, from PostgreSQL, on every keystroke.
That is a database round trip per character typed — the most frequent query the
system serves, answered by the slowest path it has.

This holds the vocabulary in a trie instead. A lookup walks one pointer per
character, so it is microseconds, and it never touches the database. The trie is
built from the index once and rebuilt when the vocabulary changes; it stores a
count per word, not the pages, so its footprint stays small.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: A prefix shorter than this matches too much to be useful.
MIN_PREFIX = 2
#: Completions returned by default.
DEFAULT_LIMIT = 6


@dataclass
class _Node:
    children: dict[str, "_Node"] = field(default_factory=dict)
    count: int = 0
    word: str = ""


class AutosuggestTrieAgent:
    """A prefix trie over the indexed vocabulary."""

    def __init__(self, max_words: int = 200_000) -> None:
        self._root = _Node()
        self._max_words = max(1, int(max_words))
        self._size = 0

    def add(self, word: str, count: int = 1) -> None:
        word = (word or "").strip().lower()
        if not word:
            return
        node = self._root
        for char in word:
            node = node.children.setdefault(char, _Node())
        if not node.word:
            if self._size >= self._max_words:
                return
            self._size += 1
        node.word = word
        node.count += max(1, int(count))

    def load(self, words) -> int:
        """Fill from (word, count) pairs. Returns how many were added."""
        added = 0
        for word, count in words:
            before = self._size
            self.add(word, count)
            added += self._size - before
        return added

    def complete(self, prefix: str, limit: int = DEFAULT_LIMIT) -> list[str]:
        """Words starting with `prefix`, most frequent first."""
        prefix = (prefix or "").strip().lower()
        if len(prefix) < MIN_PREFIX:
            return []

        node = self._root
        for char in prefix:
            node = node.children.get(char)
            if node is None:
                return []

        found: list[tuple[int, str]] = []
        self._collect(node, found, limit * 4)
        found.sort(key=lambda item: (-item[0], item[1]))
        return [word for _, word in found[:limit]]

    def _collect(self, node: _Node, out: list[tuple[int, str]], limit: int) -> None:
        """Depth-first, bounded, so one very common prefix cannot stall a request."""
        stack = [node]
        while stack and len(out) < limit:
            current = stack.pop()
            if current.word:
                out.append((current.count, current.word))
            # Sorted so the traversal is deterministic between runs.
            stack.extend(current.children[key] for key in sorted(current.children))

    def __len__(self) -> int:
        return self._size


class TrieAutosuggestAgent:
    """Keeps the trie in step with the vocabulary."""

    def __init__(self, trie: AutosuggestTrieAgent | None = None) -> None:
        self.trie = trie if trie is not None else AutosuggestTrieAgent()

    def rebuild(self, words) -> int:
        self.trie = AutosuggestTrieAgent()
        return self.trie.load(words)

    def suggest(self, prefix: str, limit: int = DEFAULT_LIMIT) -> list[str]:
        return self.trie.complete(prefix, limit)

    def stats(self) -> dict[str, object]:
        return {"words": len(self.trie), "min_prefix": MIN_PREFIX}


# ---------------------------------------------------------------------------
# Vocabulary suggestions
#
# A suggestion is a word the index contains, not a page title. A dropdown of
# sentences is not a completion, and a word read from the search vectors is
# guaranteed to match when the user accepts it.
# ---------------------------------------------------------------------------

#: Below this many characters a prefix matches too much to be a suggestion.
MIN_SUGGEST_PREFIX = 2


def suggestion_query(prefix: str) -> str:
    """The SQL that reads matching words out of the index's own vectors."""
    return (
        "SELECT word FROM ts_stat('SELECT search_vector FROM pages') "
        "WHERE word LIKE ? ORDER BY nentry DESC, word ASC LIMIT ?"
    )


def suggestion_params(prefix: str, limit: int) -> tuple[str, int]:
    """The bound parameters for `suggestion_query`."""
    return (f"{(prefix or '').strip().lower()}%", int(limit))


def is_suggestable(prefix: str) -> bool:
    """Whether a prefix is long enough to complete."""
    return len(" ".join((prefix or "").split())) >= MIN_SUGGEST_PREFIX


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "39_autosuggest_trie_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("MIN_SUGGEST_PREFIX", globals().get("MIN_SUGGEST_PREFIX"), _contract_rules.at_least(1), "MIN_SUGGEST_PREFIX must satisfy at_least(1)"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
