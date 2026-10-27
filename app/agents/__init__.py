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

__all__ = ["AGENT_MODULES", "load_agent"]

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
)


def load_agent(name: str) -> ModuleType:
    """Import one numbered agent module by its file name."""
    return import_module(f"{__name__}.{name}")
