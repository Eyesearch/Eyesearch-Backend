"""Kannada and Indic morphology, resolved locally.

Kannada is agglutinative: the same word appears as ಬೆಂಗಳೂರು, ಬೆಂಗಳೂರಿನಲ್ಲಿ,
ಬೆಂಗಳೂರಿಗೆ, ಬೆಂಗಳೂರಿನ — and a search for one finds none of the others, because
the index matches tokens, not stems.

The fix is a suffix stripper. It is a small, ordered table rather than a
morphological analyser: the endings that matter for retrieval are a closed set,
and a table that can be read and corrected beats a model that cannot. No
translation API is involved, and none is needed — nothing is being translated,
only reduced to a shared stem.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: Kannada case and plural endings, longest first so the most specific matches.
#: Ordered: ಬೆಂಗಳೂರಿನಲ್ಲಿ must lose "ನಲ್ಲಿ", not "ಲ್ಲಿ".
KANNADA_SUFFIXES: tuple[str, ...] = (
    "ನಲ್ಲಿಯೂ", "ನಲ್ಲಿನ", "ಗಳಿಂದ", "ಗಳಿಗೆ", "ಗಳಲ್ಲಿ", "ಗಿಂತಲೂ",
    "ನಲ್ಲಿಗೆ", "ನಲ್ಲಿನ", "ಗಳನ್ನ", "ಗಳಿಗೂ", "ಗಿಂತಲೂ",
    "ನಲ್ಲಿ", "ದಿಂದ", "ಗಳಿಗೆ", "ಗಳಲ್ಲಿ", "ಗಳನ್ನು", "ಗಳಿಂದ",
    "ಕ್ಕಿಂತ", "ಗೆ", "ಗೂ", "ಅನ್ನು", "ಯನ್ನು", "ನ್ನ",
    "ಗಳು", "ಗಳ", "ಗಿಂತ", "ದಲ್ಲಿ", "ದಿಂದ", "ದ", "ಯ", "ನ", "ಲಿ",
)

#: Hindi and Marathi postpositions, for the same reason.
DEVANAGARI_SUFFIXES: tuple[str, ...] = (
    "ों", "ें", "े", "ी", "ा", "ीय", "ता", "ने", "को", "से", "में", "पर", "का", "की", "के",
)

#: A stem shorter than this is not a word any more, so the suffix stays.
MIN_STEM_CHARS = 3

#: Vowel mutations that happen when a suffix is attached. ಬೆಂಗಳೂರು becomes
#: ಬೆಂಗಳೂರಿನಲ್ಲಿ, not ಬೆಂಗಳೂರುನಲ್ಲಿ: the final "ರು" turns to "ರಿ" before the
#: locative. Stripping the suffix alone leaves "ಬೆಂಗಳೂರಿ", which is not the word,
#: so the mutation is undone on the way back.
_KANNADA_MUTATIONS: tuple[tuple[str, str], ...] = (
    ("ರಿ", "ರು"),
    ("ರೆ", "ರು"),
    ("ಕ್ಕೆ", "ಕೆ"),
)

_KANNADA = re.compile(r"[\u0C80-\u0CFF]")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")


def strip_suffix(word: str) -> str:
    """The stem of a Kannada or Devanagari word, or the word unchanged."""
    if not word:
        return word

    suffixes: tuple[str, ...] = ()
    if _KANNADA.search(word):
        suffixes = KANNADA_SUFFIXES
    elif _DEVANAGARI.search(word):
        suffixes = DEVANAGARI_SUFFIXES
    else:
        return word

    for suffix in suffixes:
        if not word.endswith(suffix):
            continue
        stem = word[: -len(suffix)]
        # Stripping to two characters produces a fragment that matches
        # everything and means nothing.
        if len(stem) < MIN_STEM_CHARS:
            continue
        return _undo_mutation(stem)
    return word


def _undo_mutation(stem: str) -> str:
    """Restore the final vowel a suffix changed on its way in."""
    for mutated, original in _KANNADA_MUTATIONS:
        if stem.endswith(mutated) and len(stem) > len(mutated):
            return stem[: -len(mutated)] + original
    return stem


class IndicKannadaAgent:
    """Reduce Indic words to a shared stem for matching."""

    def stem(self, text: str) -> str:
        return " ".join(strip_suffix(token) for token in (text or "").split())

    def expand(self, text: str) -> list[str]:
        """The original tokens plus their stems, for an OR-ed query.

        Both forms are offered rather than only the stem: the index holds
        whatever the page said, and a page may well say the inflected form.
        """
        tokens = (text or "").split()
        stems = [strip_suffix(token) for token in tokens]
        seen: list[str] = []
        for token in tokens + stems:
            if token and token not in seen:
                seen.append(token)
        return seen

    def is_indic(self, text: str) -> bool:
        return bool(_KANNADA.search(text or "") or _DEVANAGARI.search(text or ""))
