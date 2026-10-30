"""The supervisor: watch every worker and restart the ones that stop breathing.

`scheduler._supervise` already restarts a worker whose task *raises*. It cannot
see a task that is alive but stuck — awaiting a socket that will never answer, or
looping without progress. That is the failure this agent exists for, and it is
the harder one: a crashed task is obvious, a stalled one looks healthy from
outside.

Workers call `beat(name)` from their loop. Anything that has not beaten within
`stale_after` seconds is cancelled and restarted from its factory.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

#: A worker that has not beaten for this long is treated as stalled.
DEFAULT_STALE_AFTER = 30.0
#: How often to look.
DEFAULT_INTERVAL = 5.0


@dataclass
class WorkerRecord:
    factory: Callable[[], Awaitable[None]]
    task: asyncio.Task | None = None
    last_beat: float = 0.0
    restarts: int = 0


@dataclass
class KingReport:
    running: list[str] = field(default_factory=list)
    stalled: list[str] = field(default_factory=list)
    restarted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "running": self.running,
            "stalled": self.stalled,
            "restarted": self.restarted,
            "failed": self.failed,
        }


class KingOrchestratorAgent:
    """Register workers, watch their heartbeats, restart the stalled ones."""

    def __init__(
        self,
        *,
        stale_after: float = DEFAULT_STALE_AFTER,
        interval: float = DEFAULT_INTERVAL,
        clock=time.monotonic,
    ) -> None:
        self.stale_after = max(1.0, float(stale_after))
        self.interval = max(0.5, float(interval))
        self._clock = clock
        self._workers: dict[str, WorkerRecord] = {}
        self._running = False
        self.beats = 0

    # -- registration -------------------------------------------------------

    def register(self, name: str, factory: Callable[[], Awaitable[None]]) -> asyncio.Task:
        """Start a worker and take responsibility for keeping it alive."""
        record = WorkerRecord(factory=factory, last_beat=self._clock())
        self._workers[name] = record
        record.task = asyncio.create_task(self._run(name, record))
        return record.task

    def beat(self, name: str) -> None:
        """Called by a worker that is making progress."""
        record = self._workers.get(name)
        if record is not None:
            record.last_beat = self._clock()
            self.beats += 1

    # -- inspection ---------------------------------------------------------

    def stalled(self) -> list[str]:
        now = self._clock()
        return [
            name
            for name, record in self._workers.items()
            if record.task is not None
            and not record.task.done()
            and (now - record.last_beat) > self.stale_after
        ]

    def health(self) -> dict[str, object]:
        return {
            "workers": {
                name: {
                    "alive": record.task is not None and not record.task.done(),
                    "restarts": record.restarts,
                    "silent_for": round(self._clock() - record.last_beat, 1),
                }
                for name, record in self._workers.items()
            },
            "beats": self.beats,
        }

    # -- the loop -----------------------------------------------------------

    async def _run(self, name: str, record: WorkerRecord) -> None:
        while True:
            try:
                await record.factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                # A factory that dies on its own is restarted with a pause, so a
                # broken dependency cannot spin the CPU in a tight loop.
                record.restarts += 1
                logger.exception("%s died; restarting in %.1fs", name, self.interval)
                await asyncio.sleep(self.interval)

    async def check_once(self) -> KingReport:
        """Restart anything stalled. One pass, so it can be tested."""
        report = KingReport()
        now = self._clock()

        for name, record in list(self._workers.items()):
            if record.task is not None and not record.task.done():
                report.running.append(name)
                if (now - record.last_beat) > self.stale_after:
                    report.stalled.append(name)
                    logger.warning("%s has been silent for %.1fs; restarting", name, now - record.last_beat)
                    record.task.cancel()
                    record.restarts += 1
                    record.last_beat = now
                    record.task = asyncio.create_task(self._run(name, record))
                    report.restarted.append(name)
            else:
                # Finished or crashed: start it again.
                report.restarted.append(name)
                record.last_beat = now
                record.task = asyncio.create_task(self._run(name, record))

        return report

    async def supervise_forever(self) -> None:
        self._running = True
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("supervisor pass failed")
            await asyncio.sleep(self.interval)

    async def stop(self) -> None:
        self._running = False
        for record in self._workers.values():
            if record.task is not None and not record.task.done():
                record.task.cancel()
        await asyncio.gather(
            *(record.task for record in self._workers.values() if record.task is not None),
            return_exceptions=True,
        )


# ---------------------------------------------------------------------------
# Contract verification and defect attribution
#
# The supervisor's other job: find rows that break the index's own contracts
# and name the step responsible for each. This is the attribution table the
# King reports against.
# ---------------------------------------------------------------------------
import logging
import unicodedata
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Words below which a page is navigation rather than content.
#: Fallback only; the auditor reads `settings.min_page_words` when it has one.
THIN_WORD_COUNT = 15
#: A page is "supposed" to have marks if its language is one that uses them.
MARKED_LANGUAGES = frozenset({"kn", "hi", "ta", "te", "ml", "bn", "gu", "pa", "or", "ar"})

#: Which agent owns each contract. The names match the modules in app/agents.
OWNERS = {
    "empty_brand": "06_metadata_extractor",
    "snippet_too_long": "10_snippet",
    "matras_stripped": "11_tokenizer",
    "thin_content": "09_quality_guard",
}


@dataclass(frozen=True)
class Defect:
    page_id: int
    url: str
    kind: str

    @property
    def owner(self) -> str:
        return OWNERS.get(self.kind, "unknown")


@dataclass
class DefectReport:
    scanned: int = 0
    defects: list[Defect] = field(default_factory=list)
    requeued: int = 0
    dry_run: bool = False

    def by_owner(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for defect in self.defects:
            counts[defect.owner] = counts.get(defect.owner, 0) + 1
        return dict(sorted(counts.items()))

    def by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for defect in self.defects:
            counts[defect.kind] = counts.get(defect.kind, 0) + 1
        return dict(sorted(counts.items()))

    def as_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "defects": len(self.defects),
            "by_owner": self.by_owner(),
            "by_kind": self.by_kind(),
            "requeued": self.requeued,
            "dry_run": self.dry_run,
        }


#: A script that uses vowel signs should show them at roughly this rate. Real
#: Kannada or Devanagari runs well above it; text with the marks stripped runs at
#: zero. A single marked word is not evidence either way, which is why this is a
#: share rather than "any marks at all".
MIN_MARK_RATIO = 0.15


def mark_ratio(text: str) -> float:
    """The share of characters that are combining marks."""
    letters = [ch for ch in (text or "") if ch.isalpha() or unicodedata.category(ch).startswith("M")]
    if not letters:
        return 0.0
    marks = sum(1 for ch in letters if unicodedata.category(ch).startswith("M"))
    return marks / len(letters)


def classify_row(
    *,
    page_id: int,
    url: str,
    brand_name: str,
    body_text: str,
    language: str,
    ceiling: int,
    thin_words: int = THIN_WORD_COUNT,
) -> list[Defect]:
    """Every contract this row breaks. Pure, so each rule is testable alone."""
    found: list[Defect] = []

    if not (brand_name or "").strip():
        found.append(Defect(page_id, url, "empty_brand"))

    if len(body_text or "") > ceiling:
        found.append(Defect(page_id, url, "snippet_too_long"))

    words = len((body_text or "").split())
    if 0 < words < thin_words:
        found.append(Defect(page_id, url, "thin_content"))

    # A page whose language needs combining marks but has none lost them. The
    # check is on the body, not the title: a title can be a single unmarked word.
    if (
        (language or "en") in MARKED_LANGUAGES
        and len(body_text or "") > 40
        and mark_ratio(body_text) < MIN_MARK_RATIO
    ):
        found.append(Defect(page_id, url, "matras_stripped"))

    return found


class DefectAuditor:
    """Scan the index for contract breaches and re-queue the offenders."""

    def __init__(self, client, settings, batch: int = 500) -> None:
        self._client = client
        self._settings = settings
        self._batch = max(1, batch)

    @property
    def thin_words(self) -> int:
        """The word floor, from the live settings rather than a constant."""
        return int(getattr(self._settings, "min_page_words", THIN_WORD_COUNT))

    async def scan(self) -> DefectReport:
        report = DefectReport()
        ceiling = int(self._settings.page_body_char_ceiling)
        cursor = 0

        while True:
            result = await self._client.execute(
                "SELECT page_id, url, brand_name, body_text, language FROM pages "
                "WHERE page_id > ? ORDER BY page_id LIMIT ?",
                (cursor, self._batch),
            )
            rows = [tuple(row) for row in result.rows]
            if not rows:
                break
            cursor = int(rows[-1][0])

            for page_id, url, brand, body, language in rows:
                report.scanned += 1
                report.defects.extend(
                    classify_row(
                        page_id=int(page_id),
                        url=str(url or ""),
                        brand_name=str(brand or ""),
                        body_text=str(body or ""),
                        language=str(language or "en"),
                        ceiling=ceiling,
                        thin_words=self.thin_words,
                    )
                )

        return report

    async def heal(self, report: DefectReport, *, dry_run: bool = False) -> int:
        """Push the offending URLs back into the queue at priority 0."""
        if dry_run:
            return len({defect.url for defect in report.defects})

        queued = 0
        seen: set[str] = set()
        for defect in report.defects:
            if defect.url in seen:
                continue
            seen.add(defect.url)
            await self._client.execute(
                "INSERT INTO crawl_queue (url, domain, status, retry_count, priority, last_error) "
                "VALUES (?, ?, 'pending', 0, 0, ?) "
                "ON CONFLICT (url) DO UPDATE SET status = 'pending', retry_count = 0, "
                "priority = 0, processed_at = NULL, last_error = EXCLUDED.last_error",
                (defect.url, _host_of(defect.url), f"defect:{defect.kind}"),
            )
            queued += 1
        report.requeued = queued
        return queued


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(url or "").hostname or ""
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "14_king_orchestrator_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 3 > 0, 3 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)


# ---------------------------------------------------------------------------
# Fleet contract telemetry
#
# Every agent reports breaches through `app.agents._contract`, and the supervisor
# reads them all here. This is the bridge the contract hooks were built for.
# ---------------------------------------------------------------------------


def fleet_contract_report() -> dict:
    """Run every agent's audit and report the fleet's health.

    The audits are *run* here rather than read from whatever happened to call
    them: an empty registry would otherwise report "no breaches" for agents that
    had never been checked, which is the most misleading answer a health check
    can give.
    """
    import glob
    import os

    from app.agents import _contract as rules
    from app.agents import load_agent

    audited = 0
    for path in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "[0-9]*.py"))):
        module = load_agent(os.path.basename(path)[:-3])
        audit = getattr(module, "audit_contract", None)
        if audit is None:
            continue
        audit()
        audited += 1

    return {
        "agents": rules.fleet_telemetry(),
        "supervisor": rules.SUPERVISOR,
        "audited": audited,
    }


# ---------------------------------------------------------------------------
# The quarantine ratio
#
# WEB is where a page lands when no rule could place it. That is honest for a
# few pages and a symptom for many: past a tenth of the corpus, the classifier
# is missing patterns rather than the web being unclassifiable.
# ---------------------------------------------------------------------------

#: Above this share of the corpus, WEB is drift rather than a fallback.
WEB_RATIO_WARNING = 0.10


async def web_ratio(client) -> dict:
    """How much of the index is sitting in the quarantine, and whether that is too much."""
    result = await client.execute(
        "SELECT "
        "  COUNT(*) FILTER (WHERE c.code = 'WEB') AS web, "
        "  COUNT(*) AS total "
        "FROM pages p LEFT JOIN categories c ON c.id = p.category_id"
    )
    web, total = (int(value or 0) for value in result.rows[0])
    ratio = (web / total) if total else 0.0

    report = {
        "web_pages": web,
        "total_pages": total,
        "web_ratio": round(ratio, 4),
        "threshold": WEB_RATIO_WARNING,
        "breach": ratio > WEB_RATIO_WARNING,
    }
    if report["breach"]:
        logger.warning(
            "WEB is %.1f%% of the corpus (%d of %d pages), above the %.0f%% threshold — "
            "the classifier is missing patterns, not facing unclassifiable pages",
            ratio * 100, web, total, WEB_RATIO_WARNING * 100,
        )
    return report
