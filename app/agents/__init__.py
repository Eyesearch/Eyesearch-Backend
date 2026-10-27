"""The six pipeline agents, addressed by number.

The directories are numbered for reading order, which makes them awkward to
import with normal syntax; this module resolves them with `importlib` so callers
can simply write `from app.agents import FrontierAgent`.
"""

from __future__ import annotations

from importlib import import_module

_frontier = import_module("app.agents.01_frontier_agent.agent")
_frontier_config = import_module("app.agents.01_frontier_agent.config")
_fetcher = import_module("app.agents.02_fetcher_agent.agent")
_parser = import_module("app.agents.03_parser_agent.agent")
_quality = import_module("app.agents.04_quality_agent.agent")
_indexer = import_module("app.agents.05_indexer_agent.agent")
_semantic = import_module("app.agents.06_semantic_ai_agent.agent")

FrontierAgent = _frontier.FrontierAgent
FrontierConfig = _frontier_config.FrontierConfig
FetcherAgent = _fetcher.FetcherAgent
FetchResult = _fetcher.FetchResult
ParserAgent = _parser.ParserAgent
ParseResult = _parser.ParseResult
QualityAgent = _quality.QualityAgent
QualityVerdict = _quality.QualityVerdict
IndexerAgent = _indexer.IndexerAgent
SemanticAIAgent = _semantic.SemanticAIAgent
SemanticResult = _semantic.SemanticResult

AGENTS = (
    "frontier",
    "fetcher",
    "parser",
    "quality",
    "indexer",
    "semantic",
)

__all__ = [
    "AGENTS",
    "FrontierAgent",
    "FrontierConfig",
    "FetcherAgent",
    "FetchResult",
    "ParserAgent",
    "ParseResult",
    "QualityAgent",
    "QualityVerdict",
    "IndexerAgent",
    "SemanticAIAgent",
    "SemanticResult",
]
