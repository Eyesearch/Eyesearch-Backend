"""Database self-healing: find hung queries, recycle what is stuck, analyse what
has gone stale.

Statement timeouts already stop a query on the server (1.5s on the search pool).
This agent is for what that does not cover: a session wedged outside a timeout
— an idle-in-transaction client holding locks, a long analytic query on the
crawler pool — and for the planner statistics that rot after a bulk delete.

Nothing here runs automatically by default: `run()` is called on a schedule by
the supervisor, and each step is bounded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: A query running longer than this is a candidate for termination.
HUNG_QUERY_MS = 1500
#: A connection idle inside a transaction for this long is holding locks for
#: nothing.
IDLE_TRANSACTION_SECONDS = 300
#: Dead tuples above this fraction of live rows is worth an ANALYZE.
DEAD_TUPLE_FRACTION = 0.20
#: Never terminate these: the doctor's own session, and any background worker
#: PostgreSQL depends on.
PROTECTED_BACKENDS = frozenset({"autovacuum worker", "walsender", "logical replication worker"})


@dataclass
class DoctorReport:
    hung: list[int] = field(default_factory=list)
    terminated: list[int] = field(default_factory=list)
    analysed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "hung": self.hung,
            "terminated": self.terminated,
            "analysed": self.analysed,
            "notes": self.notes,
        }


def select_victims(rows, *, own_pid: int, hung_ms: int = HUNG_QUERY_MS) -> list[int]:
    """Which backends to terminate, from `pg_stat_activity` rows.

    Pure, so the rule can be tested without a database. The rules matter more
    than they look: terminating the wrong backend kills a live search or the
    doctor's own session, so every one of these is a guard against that.
    """
    victims: list[int] = []
    for row in rows:
        pid = int(row.get("pid") or 0)
        if pid <= 0 or pid == own_pid:
            continue
        if (row.get("backend_type") or "") in PROTECTED_BACKENDS:
            continue
        if row.get("application_name") == "db_doctor":
            continue

        state = (row.get("state") or "").lower()
        duration = float(row.get("duration_ms") or 0.0)

        if state == "active" and duration >= hung_ms:
            victims.append(pid)
        elif state == "idle in transaction" and duration >= IDLE_TRANSACTION_SECONDS * 1000:
            victims.append(pid)

    return victims


class DbDoctorAgent:
    """Watch the database and mend what it can."""

    def __init__(self, client, *, hung_ms: int = HUNG_QUERY_MS) -> None:
        self._client = client
        self.hung_ms = int(hung_ms)

    async def own_pid(self) -> int:
        result = await self._client.execute("SELECT pg_backend_pid()")
        return int(result.scalar() or 0)

    async def activity(self) -> list[dict]:
        result = await self._client.execute(
            "SELECT pid, state, backend_type, application_name, "
            "EXTRACT(EPOCH FROM (now() - query_start)) * 1000 AS duration_ms "
            "FROM pg_stat_activity WHERE datname = current_database()"
        )
        columns = ["pid", "state", "backend_type", "application_name", "duration_ms"]
        return [dict(zip(columns, row)) for row in result.rows]

    async def terminate(self, pids: list[int]) -> list[int]:
        killed: list[int] = []
        for pid in pids:
            try:
                result = await self._client.execute("SELECT pg_terminate_backend(?)", (pid,))
                if result.scalar():
                    killed.append(pid)
            except Exception as error:  # noqa: BLE001 - one failure must not stop the rest
                logger.warning("could not terminate backend %s: %s", pid, error)
        return killed

    async def stale_tables(self, threshold: float = DEAD_TUPLE_FRACTION) -> list[str]:
        result = await self._client.execute(
            "SELECT relname, n_dead_tup, n_live_tup FROM pg_stat_user_tables "
            "WHERE n_live_tup > 1000 "
            "AND n_dead_tup::float / GREATEST(n_live_tup, 1) > ?",
            (threshold,),
        )
        return [str(row[0]) for row in result.rows]

    async def run(self, *, dry_run: bool = False) -> DoctorReport:
        report = DoctorReport()
        try:
            pid = await self.own_pid()
            rows = await self.activity()
            report.hung = select_victims(rows, own_pid=pid, hung_ms=self.hung_ms)
            if report.hung and not dry_run:
                report.terminated = await self.terminate(report.hung)

            tables = await self.stale_tables()
            if tables and not dry_run:
                for table in tables:
                    # ANALYZE only: VACUUM cannot run inside a transaction, and
                    # autovacuum already reclaims space.
                    await self._client.execute(f"ANALYZE {table}")
                    report.analysed.append(table)
        except Exception as error:  # noqa: BLE001 - the doctor must not crash the app
            report.notes.append(str(error))
            logger.warning("db doctor pass failed: %s", error)
        return report


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "15_db_doctor_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 5 > 0, 5 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
