"""The pipeline agents.

Each agent is a numbered module — `01_frontier_agent` … `08_language_agent` —
matching the brief's file names. Python cannot import a module whose name
starts with a digit using `import` syntax, so the modules are reached through
`load_agent()` here, which uses the import machinery's string form.

Nothing in this package imports the agents at import time: that keeps
`from app.agents import load_agent` free of circular imports, since every agent
module can then import this package safely.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

__all__ = ["AGENT_MODULES", "LIVE_AGENTS", "PIPELINE_AGENTS", "load_agent"]

#: Every agent module, in pipeline order.
AGENT_MODULES: tuple[str, ...] = (
    "01_frontier_agent",
    "02_fetcher_agent",
    "03_parser_agent",
    "04_quality_agent",
    "05_indexer_agent",
    "06_revalidation_agent",
    "07_classifier_agent",
    "08_language_agent",
    "09_robots_agent",
)

#: Agents outside the crawl pipeline. Listed separately so the pipeline order
#: above stays a statement about the crawl and nothing else.
LIVE_AGENTS: tuple[str, ...] = (
    # Prices, weather and headlines: a separate system from the crawl, sharing
    # only the database.
    "31_live_feed_agent",
)


#: The external fleet moved into `app.agents.federated/`, where the air gap is
#: visible in the layout rather than implied by a comment. The old numbered names
#: still resolve, because a rename that breaks every caller is a rename that gets
#: reverted — and the callers here are the scheduler and the test suite.
FEDERATED_ALIASES: dict[str, str] = {
    "41_federated_horizon_agent": "app.agents.federated.horizon_agent",
    "42_free_knowledge_courier": "app.agents.federated.knowledge_courier",
    "43_external_seed_trapper": "app.agents.federated.seed_trapper",
    "44_cross_source_sanitizer": "app.agents.federated.source_sanitizer",
    "45_live_entity_extractor": "app.agents.federated.brand_extractor_agent",
    "46_async_parallel_dispatcher": "app.agents.federated.parallel_dispatcher",
}


#: The crawl pipeline lives in `app/agents/pipeline/`, in the order a URL
#: travels through it. The files keep their numbers, and the old flat names
#: still resolve, so no caller changed when they moved.
PIPELINE_AGENTS: tuple[str, ...] = (
    "01_frontier_agent",
    "09_robots_agent",
    "13_dedupe_compact_agent",
    "02_fetcher_agent",
    "03_parser_agent",
    "10_brand_canonical_agent",
    "04_quality_agent",
    "07_classifier_agent",
    "08_language_agent",
    "05_indexer_agent",
)


def load_agent(name: str) -> ModuleType:
    """Import one agent module by its file name.

    A federated agent may be named by its old number or its new module path.
    """
    target = FEDERATED_ALIASES.get(name)
    if target:
        return import_module(target)
    if name in PIPELINE_AGENTS:
        return import_module(f"{__name__}.pipeline.{name}")
    return import_module(f"{__name__}.{name}")
