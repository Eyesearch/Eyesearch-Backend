"""The external fleet: everything that answers from outside the index.

These agents are deliberately separated from the numbered local agents, because
they obey a different contract. A local agent may write to PostgreSQL — that is
what indexing is. Nothing in here may. An external result is a guess about the
web, not a fact we crawled, and mixing the two is how a search index starts
believing things it never verified.

The air gap is the point of the directory, not an accident of layout:

- No module here imports the store, the frontier, or the crawl queue.
- `seed_trapper` is a strict no-op for external URLs: it counts what it saw and
  discards it. Its former job was to enqueue external links, which turned the
  frontier into a pipe for content we never chose to crawl.
- Results are in-memory `ExternalCard` objects. They are rendered and dropped.

Imports are absolute from the package root so a caller cannot reach past it.
"""

from __future__ import annotations

from app.agents.federated.brand_extractor_agent import (
    BrandExtraction,
    LiveEntityExtractorAgent,
    brand_from_title,
    derive_brand_from_domain,
)
from app.agents.federated.horizon_agent import FederatedHorizonAgent, FederatedResult
from app.agents.federated.knowledge_courier import FreeKnowledgeCourierAgent, KnowledgeCard
from app.agents.federated.parallel_dispatcher import AsyncParallelDispatcherAgent, DispatchResult
from app.agents.federated.seed_trapper import ExternalSeedTrapperAgent, SeedReport
from app.agents.federated.source_sanitizer import (
    ExternalCard,
    CrossSourceSanitizerAgent,
    sanitise_snippet,
    sanitise_url,
)

__all__ = [
    "AsyncParallelDispatcherAgent",
    "BrandExtraction",
    "CrossSourceSanitizerAgent",
    "DispatchResult",
    "ExternalCard",
    "ExternalSeedTrapperAgent",
    "FederatedHorizonAgent",
    "FederatedResult",
    "FreeKnowledgeCourierAgent",
    "KnowledgeCard",
    "LiveEntityExtractorAgent",
    "SeedReport",
    "brand_from_title",
    "derive_brand_from_domain",
    "sanitise_snippet",
    "sanitise_url",
]
