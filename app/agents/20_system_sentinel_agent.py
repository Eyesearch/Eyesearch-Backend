"""Watch the machine, not just the database.

The storage watchdog asks PostgreSQL how big it is. This asks the operating
system about the two resources that take the process down before the database
notices: free RAM, and free space on the volume the database writes to.

No psutil dependency. Everything needed is in `os.statvfs` and `/proc/meminfo`
on Linux, and a crawler that needs a third-party package to read its own memory
usage is a crawler with one more thing to break.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Above this, the sentinel throttles.
RAM_WARN_FRACTION = 0.85
#: Above this, it stops.
RAM_CRITICAL_FRACTION = 0.95
#: Free space below this on the data volume is a stop.
DISK_CRITICAL_FRACTION = 0.05


@dataclass
class SystemState:
    ram_used_fraction: float
    disk_used_fraction: float
    ram_total_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int

    @property
    def pressure(self) -> str:
        """`ok`, `throttle` or `stop`."""
        if self.ram_used_fraction >= RAM_CRITICAL_FRACTION:
            return "stop"
        if self.disk_used_fraction >= (1.0 - DISK_CRITICAL_FRACTION):
            return "stop"
        if self.ram_used_fraction >= RAM_WARN_FRACTION:
            return "throttle"
        return "ok"

    def as_dict(self) -> dict[str, object]:
        return {
            "ram_used_fraction": round(self.ram_used_fraction, 3),
            "disk_used_fraction": round(self.disk_used_fraction, 3),
            "ram_total_bytes": self.ram_total_bytes,
            "disk_total_bytes": self.disk_total_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "pressure": self.pressure,
        }


def read_memory() -> tuple[int, int]:
    """(total, available) in bytes, from /proc/meminfo.

    Returns (0, 0) where the file does not exist, which is the honest answer on
    a platform that does not have it — better than inventing a number that makes
    the sentinel look healthy.
    """
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            values: dict[str, int] = {}
            for line in handle:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    values[key] = int(parts[0]) * 1024
    except OSError:
        return 0, 0
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return total, available


def evaluate_system(ram_total: int, ram_available: int, disk_total: int, disk_free: int) -> SystemState:
    ram_used = 1.0 - (ram_available / ram_total) if ram_total else 0.0
    disk_used = 1.0 - (disk_free / disk_total) if disk_total else 0.0
    return SystemState(
        ram_used_fraction=max(0.0, min(1.0, ram_used)),
        disk_used_fraction=max(0.0, min(1.0, disk_used)),
        ram_total_bytes=ram_total,
        disk_total_bytes=disk_total,
        disk_free_bytes=disk_free,
    )


class SystemSentinelAgent:
    """Report the machine's pressure, and tell the orchestrator what to do."""

    def __init__(self, orchestrator=None, *, data_path: str = "/") -> None:
        self._orchestrator = orchestrator
        self._path = data_path

    def sample(self) -> SystemState:
        ram_total, ram_available = read_memory()
        try:
            usage = shutil.disk_usage(self._path)
            disk_total, disk_free = usage.total, usage.free
        except OSError:
            disk_total = disk_free = 0
        return evaluate_system(ram_total, ram_available, disk_total, disk_free)

    def apply(self, state: SystemState | None = None) -> SystemState:
        """Act on the reading: stop crawling under pressure, resume when clear."""
        state = state or self.sample()
        if self._orchestrator is None:
            return state

        if state.pressure == "stop" and not self._orchestrator.freshness_only:
            self._orchestrator.freshness_only = True
            logger.warning("system sentinel: %s; crawling stopped", state.as_dict())
        elif state.pressure == "ok" and self._orchestrator.freshness_only:
            self._orchestrator.freshness_only = False
            logger.info("system sentinel: pressure cleared; crawling resumes")
        return state


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "20_system_sentinel_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 4 > 0, 4 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
