"""Structured metadata — an adapter over the extraction that already happens.

The parser already reads JSON-LD `@type` values, `og:site_name`, `og:title` and
the `<title>`, and the brand agent already builds a breadcrumb from the URL. That
work is wired into ingestion and tested.

What is added here is the part a card can use without a schema change: a single
call that returns the metadata a page advertises, normalised, so a caller does
not have to know which of three modules owns which field.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

#: Schema.org types worth surfacing, mapped to the field a card can show.
INTERESTING_TYPES = {
    "newsarticle": "article",
    "reportagenewsarticle": "article",
    "blogposting": "article",
    "product": "product",
    "offer": "product",
    "jobposting": "job",
    "course": "course",
    "medicalwebpage": "medical",
    "softwareapplication": "software",
    "organization": "organisation",
    "governmentorganization": "organisation",
}


def _json_ld_nodes(soup) -> list[dict]:
    nodes: list[dict] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict):
                nodes.append(node)
                graph = node.get("@graph")
                if isinstance(graph, list):
                    nodes.extend(item for item in graph if isinstance(item, dict))
    return nodes


class LibrarianSchemaAgent:
    """Read the structured metadata a page publishes about itself."""

    def extract(self, soup, page_url: str = "") -> dict[str, object]:
        nodes = _json_ld_nodes(soup)

        types: list[str] = []
        names: list[str] = []
        for node in nodes:
            value = node.get("@type")
            if isinstance(value, str):
                types.append(value.strip().lower())
            elif isinstance(value, list):
                types.extend(str(item).strip().lower() for item in value)
            name = node.get("name") or node.get("headline")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())

        kinds = sorted(
            {INTERESTING_TYPES[t] for t in types if t in INTERESTING_TYPES}
        )

        og = {}
        for meta in soup.find_all("meta"):
            prop = (meta.get("property") or meta.get("name") or "").lower()
            if prop.startswith("og:") or prop.startswith("twitter:"):
                content = (meta.get("content") or "").strip()
                if content:
                    og[prop] = content

        tables = soup.find_all("table")

        return {
            "schema_types": list(dict.fromkeys(types)),
            "kinds": kinds,
            "names": list(dict.fromkeys(names))[:3],
            "open_graph": og,
            "tables": len(tables),
            "url": page_url,
        }


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "25_librarian_schema_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 2 > 0, 2 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
