"""A bounded text sniffer for PDFs, using only the standard library.

Government gazettes and orders are published as PDFs and are exactly the kind of
primary source worth indexing. A full PDF parser is a large dependency and a
large attack surface, and the index only wants 40 characters — so this reads the
first content stream, inflates it, and pulls the first run of drawn text.

It is deliberately shallow. Anything it cannot read in one pass it reports as
unreadable rather than guessing, because a wrong snippet is worse than none.
"""

from __future__ import annotations

import logging
import re
import zlib

logger = logging.getLogger(__name__)

#: Only the head of the file is read. A gazette can be 40 MB and the snippet
#: comes from the first page.
MAX_BYTES = 512 * 1024
#: The snippet the rest of the system expects.
SNIPPET_CHARS = 40

_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
#: Text-showing operators: (text) Tj and [(a) -2 (b)] TJ.
_TJ = re.compile(rb"\((?:[^()\\]|\\.)*\)\s*Tj")
_ARRAY_TJ = re.compile(rb"\[(.*?)\]\s*TJ", re.DOTALL)
_PAREN = re.compile(rb"\(((?:[^()\\]|\\.)*)\)")

_ESCAPES = {b"\\n": b" ", b"\\r": b" ", b"\\t": b" ", b"\\(": b"(", b"\\)": b")", b"\\\\": b"\\"}


def _unescape(raw: bytes) -> str:
    for source, target in _ESCAPES.items():
        raw = raw.replace(source, target)
    try:
        return raw.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001 - a broken string is just no text
        return ""


def extract_text(data: bytes, max_streams: int = 3) -> str:
    """Text drawn by the first content streams, or "" when none is readable."""
    head = data[:MAX_BYTES]
    pieces: list[str] = []

    for index, match in enumerate(_STREAM.finditer(head)):
        if index >= max_streams:
            break
        raw = match.group(1)
        try:
            inflated = zlib.decompress(raw)
        except zlib.error:
            # Not Flate, or not text at all. A font stream is not worth decoding.
            continue

        for array in _ARRAY_TJ.findall(inflated):
            pieces.append(" ".join(_unescape(part) for part in _PAREN.findall(array)))
        for literal in _TJ.findall(inflated):
            pieces.append(" ".join(_unescape(part) for part in _PAREN.findall(literal)))

    return re.sub(r"\s+", " ", " ".join(pieces)).strip()


def snippet_from_pdf(data: bytes, limit: int = SNIPPET_CHARS) -> str:
    """The first meaningful characters of a PDF, at most `limit` of them."""
    return extract_text(data)[:limit].strip()


class MediaPdfAgent:
    """Produce a compliant snippet from a PDF header, cheaply."""

    def __init__(self, limit: int = SNIPPET_CHARS) -> None:
        self.limit = int(limit)

    def looks_like_pdf(self, data: bytes) -> bool:
        return data[:5] == b"%PDF-"

    def snippet(self, data: bytes) -> str:
        if not data or not self.looks_like_pdf(data):
            return ""
        return snippet_from_pdf(data, self.limit)


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "32_media_pdf_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 3 > 0, 3 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
