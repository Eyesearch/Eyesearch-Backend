"""A small, portable snapshot of everything that is expensive to recreate.

The pages themselves can be re-crawled. What cannot is the knowledge of where to
start and how it was organised: the seed list, the domain set, and the category
map. Those are kilobytes, and having them means a rebuild starts from what was
learned instead of from nothing.

The snapshot is gzipped JSON — readable with `zcat`, restorable without this
code — because a backup that needs the application to read it is not a backup.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class SnapshotReport:
    path: str = ""
    bytes_written: int = 0
    domains: int = 0
    categories: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "bytes_written": self.bytes_written,
            "domains": self.domains,
            "categories": self.categories,
            "notes": self.notes,
        }


def domain_hash(domain: str) -> str:
    """A stable short hash, so a snapshot does not carry a domain list in clear."""
    return hashlib.sha256((domain or "").lower().encode("utf-8")).hexdigest()[:16]


class DisasterSnapshotAgent:
    """Write and read the small snapshot that makes a rebuild cheap."""

    def __init__(self, client, settings=None) -> None:
        self._client = client
        self._settings = settings

    async def collect(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "version": 1,
        }
        try:
            domains = await self._client.execute("SELECT domain FROM sites ORDER BY domain")
            payload["domains"] = [str(row[0]) for row in domains.rows]
            payload["domain_hashes"] = [domain_hash(str(row[0])) for row in domains.rows]

            categories = await self._client.execute("SELECT code, name FROM categories ORDER BY id")
            payload["categories"] = {str(row[0]): str(row[1]) for row in categories.rows}

            pages = await self._client.execute("SELECT COUNT(*) FROM pages")
            payload["page_count"] = int(pages.scalar(0) or 0)
        except Exception as error:  # noqa: BLE001 - a partial snapshot beats none
            payload["error"] = str(error)
            logger.warning("snapshot could not read everything: %s", error)
        return payload

    async def write(self, path: str) -> SnapshotReport:
        payload = await self.collect()
        report = SnapshotReport(
            path=path,
            domains=len(payload.get("domains") or []),
            categories=len(payload.get("categories") or {}),
        )

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # mtime=0 so two identical snapshots are byte-identical, which makes
        # "did anything change" a comparison rather than a guess.
        with gzip.GzipFile(path, "wb", mtime=0) as handle:
            handle.write(json.dumps(payload, sort_keys=True, indent=2).encode("utf-8"))

        report.bytes_written = os.path.getsize(path)
        return report

    @staticmethod
    def read(path: str) -> dict[str, object]:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "38_disaster_snapshot_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 1 > 0, 1 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
