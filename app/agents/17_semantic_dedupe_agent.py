"""Find near-duplicate pages across domains, not just identical ones.

The existing dedupe compares exact digests and canonical paths. That misses the
common case: the same press release republished by ten outlets, each with its
own URL, its own digest and a different byline. The text is the same page.

SimHash answers that cheaply. Each page's body is hashed to 64 bits such that
similar texts land close together, and two pages within a small Hamming distance
are near-duplicates. It is a fingerprint, not a comparison of every pair — which
is what makes it usable over a whole index.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Bits in the fingerprint.
HASH_BITS = 64
#: Fingerprints within this many bits are the same content, reworded.
#: Measured with three-word shingles: a sentence with one word changed came out
#: 9 bits apart, a syndicated copy carrying an added byline 6, and two unrelated
#: passages 26. 10 sits in the gap. Too tight and syndicated copies survive; too
#: loose and unrelated pages are deleted, which cannot be undone.
NEAR_DUPLICATE_DISTANCE = 10
#: Fingerprint bands. Pages only compared within a band, which is what turns an
#: all-pairs comparison into a lookup.
BAND_BITS = 16

_TOKEN = re.compile(r"[a-z0-9]+")

#: Words per shingle. Hashing single words makes the fingerprint far too
#: sensitive — changing one word in a sentence moved eight bits, which no
#: sensible threshold would catch. Three-word shingles carry word order and
#: survive small edits.
SHINGLE_WORDS = 3


def _tokens(text: str) -> list[str]:
    """Three-word shingles of the text, which is what gets hashed."""
    words = _TOKEN.findall((text or "").lower())
    if len(words) < SHINGLE_WORDS:
        return words
    return [
        " ".join(words[index : index + SHINGLE_WORDS])
        for index in range(len(words) - SHINGLE_WORDS + 1)
    ]


def simhash(text: str, bits: int = HASH_BITS) -> int:
    """A 64-bit fingerprint: similar texts give similar bits."""
    tokens = _tokens(text)
    if not tokens:
        return 0

    weights = [0] * bits
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(bits):
            weights[bit] += 1 if (value >> bit) & 1 else -1

    fingerprint = 0
    for bit in range(bits):
        if weights[bit] > 0:
            fingerprint |= 1 << bit
    return fingerprint


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & ((1 << HASH_BITS) - 1)).count("1")


def bands(fingerprint: int, band_bits: int = BAND_BITS) -> list[int]:
    """The fingerprint split into bands, so lookups can use a dict."""
    mask = (1 << band_bits) - 1
    return [
        (fingerprint >> shift) & mask
        for shift in range(0, HASH_BITS, band_bits)
    ]


@dataclass
class SemanticReport:
    inspected: int = 0
    near_duplicates: int = 0
    removed: int = 0
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "inspected": self.inspected,
            "near_duplicates": self.near_duplicates,
            "removed": self.removed,
            "dry_run": self.dry_run,
            "notes": self.notes,
        }


class SemanticDedupeAgent:
    """Keep the best copy of syndicated content and drop the rest."""

    def __init__(self, client, *, distance: int = NEAR_DUPLICATE_DISTANCE, batch: int = 500) -> None:
        self._client = client
        self.distance = int(distance)
        self.batch = max(1, int(batch))

    async def run(self, *, dry_run: bool = False) -> SemanticReport:
        report = SemanticReport(dry_run=dry_run)
        #: band value -> [(page_id, fingerprint, quality)]
        index: dict[int, list[tuple[int, int, float]]] = {}
        doomed: list[int] = []
        cursor = 0

        while True:
            result = await self._client.execute(
                "SELECT page_id, body_text, quality_score FROM pages "
                "WHERE page_id > ? ORDER BY page_id LIMIT ?",
                (cursor, self.batch),
            )
            rows = [tuple(row) for row in result.rows]
            if not rows:
                break
            cursor = int(rows[-1][0])

            for page_id, body_text, quality in rows:
                report.inspected += 1
                fingerprint = simhash(str(body_text or ""))
                if fingerprint == 0:
                    continue
                score = float(quality or 0.0)
                matched = False

                for band in bands(fingerprint):
                    for other_id, other_fp, other_score in index.get(band, ()):
                        if hamming(fingerprint, other_fp) > self.distance:
                            continue
                        # Near-duplicates: the better copy stays.
                        if score > other_score:
                            doomed.append(other_id)
                        else:
                            doomed.append(int(page_id))
                        report.near_duplicates += 1
                        matched = True
                        break
                    if matched:
                        break

                if not matched:
                    for band in bands(fingerprint):
                        index.setdefault(band, []).append((int(page_id), fingerprint, score))

        if doomed and not dry_run:
            unique = sorted(set(doomed))
            for start in range(0, len(unique), self.batch):
                chunk = unique[start : start + self.batch]
                placeholders = ", ".join("?" for _ in chunk)
                await self._client.execute(
                    f"DELETE FROM pages WHERE page_id IN ({placeholders})", tuple(chunk)
                )
            report.removed = len(unique)

        return report
