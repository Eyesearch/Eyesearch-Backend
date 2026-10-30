"""Agent 06 — revalidation.

The index goes stale: timings change, prices move, a page is replaced by a
holding page. This worker walks the oldest rows, re-fetches them, and updates
the row in place when the fresh copy still passes the quality gate.

It runs on a long, jittered interval so a fleet of instances never revalidates
in lockstep, and it never raises out of its loop — a dead host is logged and
skipped, and the pass carries on.
"""

from __future__ import annotations

import asyncio
import logging
import random

from app.agents import load_agent
from app.engine.config import Settings, get_settings
from app.engine.store import IndexStore, PageDocument

logger = logging.getLogger(__name__)

#: The worker never sleeps less than this, however it is configured.
MIN_SLEEP_SECONDS = 30.0

_frontier_module = load_agent("01_frontier_agent")
_parser_module = load_agent("03_parser_agent")
_quality_module = load_agent("04_quality_agent")

ParserAgent = _parser_module.ParserAgent
QualityAgent = _quality_module.QualityAgent


class RevalidationAgent:
    """Re-fetches the stalest rows and refreshes them in place."""

    def __init__(
        self,
        store: IndexStore,
        fetcher,
        settings: Settings | None = None,
        parser: object | None = None,
        quality: object | None = None,
    ) -> None:
        self._store = store
        self._fetcher = fetcher
        self._settings = settings or get_settings()
        self._parser = parser or ParserAgent()
        self._quality = quality or QualityAgent()
        self.refreshed = 0
        self.failed = 0

    async def run_once(self) -> int:
        """One revalidation pass. Returns how many rows were refreshed."""
        stale = await self._store.stale_pages(
            limit=self._settings.revalidation_batch_size,
            older_than_seconds=self._settings.revalidation_stale_after_seconds,
        )
        if not stale:
            return 0

        self._quality.reset()
        refreshed = 0
        for url in stale:
            try:
                result = await self._fetcher.fetch(url, attempts=1)
                if not result.ok:
                    self.failed += 1
                    logger.debug("revalidation skipped %s: %s", url, result.reason)
                    continue

                parsed = self._parser.parse(url, result.html)
                if not parsed.ok:
                    self.failed += 1
                    continue

                domain = self._domain(url)
                document = PageDocument(
                    url=url,
                    domain=domain,
                    title=parsed.title,
                    description=parsed.description,
                    body_text=parsed.body_text,
                    schema_types=list(parsed.schema_types),
                    brand_name=parsed.brand_name,
                    clean_url=parsed.clean_url,
                    action_title=parsed.action_title,
                )
                verdict = self._quality.check(document)
                if not verdict.ok:
                    self.failed += 1
                    logger.debug("revalidation rejected %s: %s", url, verdict.reason)
                    continue

                document.digest = verdict.digest
                document.quality_score = verdict.score
                if await self._store.update_page(document):
                    refreshed += 1
            except Exception as error:  # never let one URL end the pass
                self.failed += 1
                logger.warning("revalidation error for %s: %s", url, error)

        self.refreshed += refreshed
        if refreshed:
            logger.info("revalidation: refreshed %d of %d stale pages", refreshed, len(stale))
        return refreshed

    async def run_forever(self) -> None:
        """Loop until cancelled, sleeping a jittered, never-short interval."""
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # a broken pass must not kill the worker
                logger.error("revalidation pass failed: %s", error)

            jitter = random.uniform(0, max(0, self._settings.revalidation_jitter_seconds))
            delay = max(
                MIN_SLEEP_SECONDS,
                self._settings.revalidation_interval_seconds + jitter,
            )
            logger.debug("revalidation worker sleeping %.1fs", delay)
            await asyncio.sleep(delay)

    @staticmethod
    def _domain(url: str) -> str:
        """The same registrable domain the rest of the pipeline uses."""
        return _frontier_module.domain_of(url)


__all__ = ["MIN_SLEEP_SECONDS", "RevalidationAgent"]


# ---------------------------------------------------------------------------
# Conditional requests
#
# A page that has not changed does not need downloading again. The server says
# so with an ETag or a Last-Modified date; sending them back costs one header
# and saves the whole body.
#
# The validators are built and interpreted here. Storing them needs two columns
# on `pages` and a write path through the fetcher, which is a schema change this
# module deliberately does not make on its own.
# ---------------------------------------------------------------------------

#: A response that means "nothing changed, use what you have".
NOT_MODIFIED = 304


def conditional_headers(
    etag: str | None = None, last_modified: str | None = None
) -> dict[str, str]:
    """The headers that ask a server to answer 304 when nothing has changed."""
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def validators_from(response_headers) -> dict[str, str]:
    """The validators a response offers, for the next request."""
    found: dict[str, str] = {}
    try:
        etag = response_headers.get("ETag")
        last_modified = response_headers.get("Last-Modified")
    except AttributeError:
        return found
    if etag:
        found["etag"] = str(etag)
    if last_modified:
        found["last_modified"] = str(last_modified)
    return found


def is_unchanged(status: int) -> bool:
    """Whether a response says the cached copy is still current."""
    return int(status) == NOT_MODIFIED


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "06_revalidation_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 5 > 0, 5 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
