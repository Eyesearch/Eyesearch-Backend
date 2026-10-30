"""Spider-trap defence — an adapter over the rules that already exist.

The trap rules live in `01_frontier_agent.py`: `TRAP_PATH_SUBSTRINGS` for error
pages and calendar grids, `TRACKING_PARAMS` for campaign and session keys,
`AUTH_PATH_SEGMENTS` for login and registration forms, plus the date-archive and
deep-pagination checks inside `is_crawlable`. They are applied to every candidate
before it can enter `crawl_queue`, which is the only place the decision needs to
be made.

Re-implementing those patterns here would create two lists to keep in step, and
the one that is not consulted is the one that goes stale. This agent therefore
delegates: it exists so the architecture has a named defence layer, and so
callers can ask the question without knowing where the rules live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from app.agents import load_agent

_frontier = load_agent("01_frontier_agent")

is_crawlable = _frontier.is_crawlable
TRAP_PATH_SUBSTRINGS = _frontier.TRAP_PATH_SUBSTRINGS
TRACKING_PARAMS = _frontier.TRACKING_PARAMS
AUTH_PATH_SEGMENTS = _frontier.AUTH_PATH_SEGMENTS


@dataclass
class ShieldReport:
    inspected: int = 0
    blocked: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "inspected": self.inspected,
            "blocked": self.blocked,
            "reasons": self.reasons,
        }


def classify_trap(url: str) -> str:
    """Why a URL is a trap, or an empty string when it is fine."""
    lowered = (url or "").lower()
    if not lowered:
        return "empty"

    for marker in TRAP_PATH_SUBSTRINGS:
        if marker in lowered:
            return f"path:{marker}"

    # Parsed, not substring-matched: "utm_source=" also contains "source=", so a
    # substring test reports the wrong parameter — and would miss any key that
    # only appears as part of a longer one.
    try:
        params = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    except ValueError:
        params = {}
    for key in params:
        if key.lower() in TRACKING_PARAMS:
            return f"tracking:{key.lower()}"

    for segment in AUTH_PATH_SEGMENTS:
        if f"/{segment}" in lowered:
            return f"auth:{segment}"

    if not is_crawlable(url):
        return "not_crawlable"

    return ""


class TrapShieldAgent:
    """Decide whether a URL may enter the queue, and say why not."""

    def check(self, url: str) -> str:
        return classify_trap(url)

    def screen(self, urls) -> ShieldReport:
        report = ShieldReport()
        for url in urls:
            report.inspected += 1
            reason = classify_trap(url)
            if reason:
                report.blocked += 1
                report.reasons[reason] = report.reasons.get(reason, 0) + 1
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
    "agent": "18_trap_shield_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("TRAP_PATH_SUBSTRINGS", globals().get("TRAP_PATH_SUBSTRINGS"), _contract_rules.has_items(1), "TRAP_PATH_SUBSTRINGS must satisfy has_items(1)"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
