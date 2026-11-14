"""The index and the live facts stay apart, and this test is what keeps them so.

A comment saying "do not mix these" is a comment. This fails the build instead.
The two systems may share a database and nothing else: if live code starts
importing the crawler, a live change can break indexing, which is the one thing
the split exists to prevent.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Modules that belong to the crawl: engine, the pipeline agents, the indexer.
INDEX_PACKAGES = ("app.engine", "app.agents", "app.indexer", "app.crawler")


def imports_in(path: Path) -> set[str]:
    """Every dotted module name a file imports, including inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_live_never_imports_the_index():
    live_files = list((ROOT / "app" / "live").rglob("*.py"))
    assert live_files, "the live package should exist"

    offenders: list[str] = []
    for path in live_files:
        for module in imports_in(path):
            if module.startswith(INDEX_PACKAGES):
                offenders.append(f"{path.relative_to(ROOT)} imports {module}")

    assert not offenders, "live code must not depend on the crawl:\n" + "\n".join(offenders)


def test_the_index_never_imports_live():
    """The one exception is the agent module, which the scheduler loads by name.

    Reaching it through `load_agent` rather than an import keeps the dependency
    at the scheduler's boundary instead of inside the crawl pipeline.
    """
    offenders: list[str] = []
    for package in ("engine", "indexer", "crawler", "agents"):
        for path in (ROOT / "app" / package).rglob("*.py"):
            if path.name == "31_live_feed_agent.py":
                continue
            for module in imports_in(path):
                if module == "app.live" or module.startswith("app.live."):
                    offenders.append(f"{path.relative_to(ROOT)} imports {module}")

    assert not offenders, "the crawl must not depend on live facts:\n" + "\n".join(offenders)


def test_the_live_agent_is_outside_the_crawl_pipeline():
    """It is listed in LIVE_AGENTS, not in the pipeline's ordered module list."""
    from app.agents import AGENT_MODULES, LIVE_AGENTS

    assert "31_live_feed_agent" in LIVE_AGENTS
    assert "31_live_feed_agent" not in AGENT_MODULES
