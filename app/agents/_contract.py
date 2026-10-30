"""The contract every agent declares and can be held to.

Each agent states its own rules as data — a threshold and the invariant that
threshold must satisfy — and gets three things from here:

* `audit_contract()`, which checks those invariants against the module's live
  values and returns a report. It fails when someone edits a constant into a
  state that breaks the rule, which is the point: a threshold of zero words is
  not a tuning decision, it is a bug.
* `telemetry()`, which exposes the breaches recorded against this agent.
* A shared registry, so agent 14 (the supervisor) can read every agent's health
  in one place instead of each agent inventing a way to report.

The checks are deliberately about the agent's *own* constants. A contract that
could only ever pass would be decoration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Where breaches are recorded, keyed by agent module name. Agent 14 reads this.
REGISTRY: dict[str, list["ContractBreach"]] = {}

#: The agent every breach is reported to.
SUPERVISOR = "14_king_orchestrator_agent"


@dataclass(frozen=True)
class Rule:
    """One invariant an agent holds itself to."""

    name: str
    value: Any
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class ContractBreach:
    agent: str
    rule: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"agent": self.agent, "rule": self.rule, "detail": self.detail}


@dataclass
class ContractReport:
    agent: str
    rules: list[Rule] = field(default_factory=list)

    @property
    def breaches(self) -> list[Rule]:
        return [rule for rule in self.rules if not rule.ok]

    @property
    def ok(self) -> bool:
        return not self.breaches

    def as_dict(self) -> dict[str, object]:
        return {
            "agent": self.agent,
            "checked": len(self.rules),
            "ok": self.ok,
            "breaches": [
                {"rule": rule.name, "value": rule.value, "detail": rule.detail}
                for rule in self.breaches
            ],
        }


# -- invariant builders ------------------------------------------------------


def at_least(minimum: float) -> Callable[[Any], bool]:
    return lambda value: isinstance(value, (int, float)) and value >= minimum


def at_most(maximum: float) -> Callable[[Any], bool]:
    return lambda value: isinstance(value, (int, float)) and value <= maximum


def between(low: float, high: float) -> Callable[[Any], bool]:
    return lambda value: isinstance(value, (int, float)) and low <= value <= high


def non_empty() -> Callable[[Any], bool]:
    return lambda value: bool(value)


def has_items(count: int) -> Callable[[Any], bool]:
    return lambda value: hasattr(value, "__len__") and len(value) >= count


# -- the two methods every agent gets ---------------------------------------


def make_audit_contract(module_name: str, contract: dict) -> Callable[[], ContractReport]:
    """Build this agent's `audit_contract()` from its declared rules."""

    def audit_contract() -> ContractReport:
        report = ContractReport(agent=contract.get("agent", module_name))
        for rule in contract.get("rules", []):
            report.rules.append(
                Rule(
                    name=rule.name,
                    value=rule.value,
                    ok=bool(rule.ok),
                    detail=rule.detail,
                )
            )

        breaches = [
            ContractBreach(report.agent, rule.name, rule.detail)
            for rule in report.breaches
        ]
        REGISTRY[report.agent] = breaches
        for breach in breaches:
            logger.warning("contract breach in %s: %s (%s)", breach.agent, breach.rule, breach.detail)
        return report

    return audit_contract


def make_telemetry(module_name: str, contract: dict) -> Callable[[], dict]:
    """Build this agent's `telemetry()`: what it reports to the supervisor."""

    def telemetry() -> dict:
        agent = contract.get("agent", module_name)
        breaches = REGISTRY.get(agent, [])
        return {
            "agent": agent,
            "reports_to": contract.get("reports_to", SUPERVISOR),
            "breaches": len(breaches),
            "last_breaches": [breach.as_dict() for breach in breaches[-5:]],
        }

    return telemetry


def fleet_telemetry() -> dict[str, int]:
    """Every agent's breach count, for the supervisor's report."""
    return {agent: len(breaches) for agent, breaches in sorted(REGISTRY.items())}
