"""Agent 08 — the language agent.

Detects the script a page is written in, so a search can be filtered by
language. Three outcomes: `kn` (Kannada), `hi` (Devanagari) and `en` (Latin,
the fallback).

Detection is by Unicode block, not by word lists: a page in Kannada is written
in the Kannada block whatever its vocabulary, and no dictionary is needed. A
script has to carry a real share of the text before it wins, so an English page
with a Kannada menu does not get labelled Kannada.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Unicode blocks, inclusive.
SCRIPT_RANGES: dict[str, tuple[int, int]] = {
    "kn": (0x0C80, 0x0CFF),
    "hi": (0x0900, 0x097F),
}

DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "hi", "kn")

#: A script must be at least this share of the letters to be the language.
MIN_SCRIPT_SHARE = 0.15

#: ...and at least this many characters, so a stray glyph does not decide it.
MIN_SCRIPT_CHARS = 3


@dataclass
class Detection:
    """The detected language and the evidence for it."""

    language: str = DEFAULT_LANGUAGE
    counts: dict[str, int] | None = None
    share: float = 0.0

    @property
    def is_fallback(self) -> bool:
        return self.language == DEFAULT_LANGUAGE and self.share == 0.0


class LanguageAgent:
    """Detects `kn`, `hi` or `en` from the characters actually present."""

    def detect(self, text: str) -> str:
        return self.detect_detail(text).language

    def detect_detail(self, text: str) -> Detection:
        if not text:
            return Detection(language=DEFAULT_LANGUAGE, counts={}, share=0.0)

        counts = {language: 0 for language in SCRIPT_RANGES}
        letters = 0

        for character in text:
            if not character.isalpha():
                continue
            letters += 1
            codepoint = ord(character)
            for language, (low, high) in SCRIPT_RANGES.items():
                if low <= codepoint <= high:
                    counts[language] += 1
                    break

        if not letters:
            return Detection(language=DEFAULT_LANGUAGE, counts=counts, share=0.0)

        language, count = max(counts.items(), key=lambda item: item[1])
        share = count / letters

        if count >= MIN_SCRIPT_CHARS and share >= MIN_SCRIPT_SHARE:
            return Detection(language=language, counts=counts, share=round(share, 4))

        return Detection(language=DEFAULT_LANGUAGE, counts=counts, share=round(share, 4))


__all__ = [
    "DEFAULT_LANGUAGE",
    "Detection",
    "LanguageAgent",
    "MIN_SCRIPT_CHARS",
    "MIN_SCRIPT_SHARE",
    "SCRIPT_RANGES",
    "SUPPORTED_LANGUAGES",
]
