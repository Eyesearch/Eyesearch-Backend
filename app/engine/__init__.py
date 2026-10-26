"""EyeSearch's own index: extraction, storage, ranking and local search.

This package is the proprietary half of the gateway. It owns the documents the
crawler has fetched, the FTS5 index over them, and the ranking that decides
whether a query can be answered from our own index or must fall back to the
external aggregator.

`app.engine.store` is deliberately *not* imported here: it depends on
`app.database`, which imports the schema from this package, and a package-level
import would make that circular. Import the store directly where you need it:

    from app.engine.store import get_store
"""

from app.engine.config import EngineSettings, get_engine_settings
from app.engine.ranking import rank_documents
from app.engine.schema import BM25_WEIGHTS, ENGINE_SCHEMA, FTS_COLUMNS
from app.engine.text import ExtractedDocument, extract_document

__all__ = [
    "EngineSettings",
    "get_engine_settings",
    "rank_documents",
    "ENGINE_SCHEMA",
    "FTS_COLUMNS",
    "BM25_WEIGHTS",
    "ExtractedDocument",
    "extract_document",
]
