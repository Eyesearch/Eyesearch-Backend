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

#: Unicode blocks, inclusive. One range per script, covering the major Indian
#: languages plus the Arabic block that carries Urdu.
SCRIPT_RANGES: dict[str, tuple[int, int]] = {
    "kn": (0x0C80, 0x0CFF),  # Kannada
    "hi": (0x0900, 0x097F),  # Devanagari — Hindi, Marathi, Sanskrit, Nepali
    "ta": (0x0B80, 0x0BFF),  # Tamil
    "te": (0x0C00, 0x0C7F),  # Telugu
    "ml": (0x0D00, 0x0D7F),  # Malayalam
    "bn": (0x0980, 0x09FF),  # Bengali and Assamese
    "gu": (0x0A80, 0x0AFF),  # Gujarati
    "pa": (0x0A00, 0x0A7F),  # Gurmukhi — Punjabi
    "or": (0x0B00, 0x0B7F),  # Odia
    "ar": (0x0600, 0x06FF),  # Arabic — Urdu and Kashmiri
}

DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES: tuple[str, ...] = (
    "en",
    "hi",
    "kn",
    "ta",
    "te",
    "ml",
    "bn",
    "gu",
    "pa",
    "or",
    "ar",
)

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
            codepoint = ord(character)
            matched = ""
            for language, (low, high) in SCRIPT_RANGES.items():
                if low <= codepoint <= high:
                    matched = language
                    break

            if matched:
                # Count the character even when it is a combining mark:
                # `isalpha()` rejects vowel signs, so a short Bengali or Tamil
                # word would otherwise never reach the minimum character count.
                counts[matched] += 1
                letters += 1
            elif character.isalpha():
                letters += 1

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


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "08_language_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("SCRIPT_RANGES", globals().get("SCRIPT_RANGES"), _contract_rules.has_items(8), "SCRIPT_RANGES must satisfy has_items(8)"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
