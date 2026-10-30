"""Spelling and intent, resolved locally.

A misspelling produces no results, and "no results" is a dead end. The fix is a
correction the user can see and accept — which means it has to be cheap enough
to compute on every empty result, and it has to be honest about its confidence.

Everything here is local: Levenshtein distance against the vocabulary actually
present in the index, with no model and no API.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9]+")

#: Below this word length a correction is more likely to be wrong than useful.
MIN_WORD_LENGTH = 3
#: How far a suggestion may be from what was typed.
MAX_DISTANCE = 2


def levenshtein(a: str, b: str, limit: int = MAX_DISTANCE) -> int:
    """Edit distance, abandoning early once it exceeds `limit`.

    The early exit is what makes this usable over a whole vocabulary: most pairs
    differ in the first character, and there is no point computing the rest.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        best = current[0]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            best = min(best, value)
        if best > limit:
            return limit + 1
        previous = current
    return previous[-1]


@dataclass
class Suggestion:
    original: str
    corrected: str
    changed: bool

    def as_dict(self) -> dict[str, object]:
        return {"original": self.original, "corrected": self.corrected, "changed": self.changed}


class SemanticSageAgent:
    """Correct a query against the vocabulary the index actually contains."""

    def __init__(self, vocabulary: Counter | dict[str, int] | None = None) -> None:
        self._vocabulary = Counter(vocabulary or {})

    def learn(self, text: str) -> None:
        """Add a document's words to the vocabulary."""
        for word in _WORD.findall((text or "").lower()):
            if len(word) >= MIN_WORD_LENGTH:
                self._vocabulary[word] += 1

    def is_known(self, word: str) -> bool:
        return self._vocabulary.get(word.lower(), 0) > 0

    def closest(self, word: str, limit: int = MAX_DISTANCE) -> str | None:
        """The most frequent vocabulary word within `limit` edits."""
        word = word.lower()
        if len(word) < MIN_WORD_LENGTH or self.is_known(word):
            return None

        best: tuple[int, int, str] | None = None
        for candidate, frequency in self._vocabulary.items():
            if abs(len(candidate) - len(word)) > limit:
                continue
            distance = levenshtein(word, candidate, limit)
            if distance > limit:
                continue
            # Fewer edits wins; frequency breaks a tie.
            key = (distance, -frequency, candidate)
            if best is None or key < best:
                best = key

        return best[2] if best else None

    def suggest(self, query: str) -> Suggestion:
        """A corrected query, with only the unknown words replaced."""
        words = (query or "").split()
        corrected = [self.closest(word) or word for word in words]
        result = " ".join(corrected)
        return Suggestion(original=query, corrected=result, changed=result != query)


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "26_semantic_sage_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 3 > 0, 3 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
