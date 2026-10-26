"""The universal ingestion pipeline: no hardcoding, verified extracts only.

Every assertion here is built from arbitrary queries — the point of the task was
that nothing may be special-cased for a particular word.
"""

from __future__ import annotations

import pytest

from app import database
from app.config import Settings
from app.engine.relevance import coverage, is_relevant
from app.engine.store import get_store
from app.services import live_retrieval, search_service
from app.services.live_retrieval import RawResult, RetrievalOutcome

CASES = [
    # (query, title, url, snippet, expected)
    ("quantum computing", "Quantum computing — overview", "https://example.in/q", "", True),
    ("quantum computing", "Quantum computing", "https://example.in/q", "Qubits and gates.", True),
    ("quantum computing", "Computing news", "https://example.in/news", "A story about solace.", False),
    ("namma metro timings", "Namma Metro timings", "https://bmrc.co.in/t", "", True),
    ("namma metro timings", "Namma Metro", "https://bmrc.co.in/m", "Timings and fares.", True),
    ("namma metro timings", "Metro", "https://bmrc.co.in/m", "Fares.", False),
    ("ಕನ್ನಡ ಸಿನಿಮಾ", "ಕನ್ನಡ ಸಿನಿಮಾ ವಿಮರ್ಶೆ", "https://example.in/kn", "", True),
    ("ಕನ್ನಡ ಸಿನಿಮಾ", "ಇತರೆ ವಿಷಯ", "https://example.in/other", "", False),
    ("rcb", "Royal Challengers (RCB) squad", "https://example.in/rcb", "", True),
    ("rcb", "Phone Link pairing", "https://example.in/ms", "Unrelated.", False),
]


@pytest.mark.parametrize("query,title,url,snippet,expected", CASES)
def test_relevance_gate_is_generic(query, title, url, snippet, expected) -> None:
    """60% of the query's words must appear in title/URL/snippet — for any query."""
    assert is_relevant(query, title=title, url=url, snippet=snippet) is expected


def test_coverage_is_a_fraction_not_a_verdict() -> None:
    assert coverage("alpha beta gamma delta", title="alpha beta") == 0.5
    assert coverage("alpha", title="alpha") == 1.0
    assert coverage("alpha", title="") == 0.0
    assert coverage("", title="alpha") == 0.0


@pytest.fixture()
async def temp_db(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/pipeline.db")
    await database.connect(settings)
    await database.initialize_schema()
    await get_store().ensure_schema()
    yield
    await database.close()


async def test_pipeline_serves_our_extract_not_the_provider_text(temp_db, monkeypatch) -> None:
    async def _live(query, limit, **kwargs):
        return RetrievalOutcome(
            results=[
                RawResult(
                    title="PROVIDER RAW TITLE",
                    url="https://example.in/quantum-overview",
                    # The candidate itself must pass the gate before it is fetched.
                    snippet="quantum computing provider snippet",
                    source="duckduckgo",
                )
            ],
            providers={"duckduckgo": 1},
        )

    async def _extract(urls, *, client=None, settings=None, limit=None):
        from app.engine.text import ExtractedDocument

        return [
            ExtractedDocument(
                url="https://example.in/quantum-overview",
                canonical_url="https://example.in/quantum-overview",
                domain="example.in",
                root_domain="example.in",
                domain_class="static",
                title="Quantum computing: a clean extract",
                snippet="Qubits, gates and error correction.",
                content="Quantum computing explained from our own extractor.",
                language="en",
                favicon_url=None,
            )
        ]

    monkeypatch.setattr(live_retrieval, "retrieve", _live)
    monkeypatch.setattr(search_service, "extract_candidates", _extract)

    service = search_service.SearchService(client=None, settings=Settings(turso_db_url="file:unused"))
    first = await service.search("quantum computing", limit=5)

    assert first.source == "index"
    assert first.results[0].title == "Quantum computing: a clean extract"
    assert "PROVIDER RAW TITLE" not in [result.title for result in first.results]

    stored = await get_store().get_document("https://example.in/quantum-overview")
    assert stored is not None
    assert stored["title"] == "Quantum computing: a clean extract"
    assert "provider snippet" not in str(stored["snippet"])

    # And the next identical query is answered from documents_fts.
    second = await service.search("quantum computing", limit=5)
    assert second.source == "index"
    assert second.index_hits >= 1


async def test_repeat_extraction_upserts_in_place(temp_db) -> None:
    store = get_store()
    base = {
        "url": "https://example.in/page",
        "canonical_url": "https://example.in/page",
        "domain": "example.in",
        "root_domain": "example.in",
        "domain_class": "static",
        "snippet": "first version",
        "content": "first version of the page",
        "language": "en",
        "favicon_url": None,
        "payload_bytes": 120,
    }

    first = await store.ingest_documents([{**base, "title": "Page v1"}], mode="upsert")
    assert first["inserted"] == 1

    second = await store.ingest_documents([{**base, "title": "Page v2"}], mode="upsert")
    assert second["replaced"] == 1
    assert second["inserted"] == 0
    assert await store.count_documents() == 1

    stored = await store.get_document("https://example.in/page")
    assert stored is not None and stored["title"] == "Page v2"

    # The crawler's own rule is unchanged: a plain insert rejects the duplicate.
    third = await store.ingest_documents([{**base, "title": "Page v3"}], mode="insert")
    assert third["duplicate"] == 1


async def test_index_hit_never_touches_the_providers(temp_db, monkeypatch) -> None:
    store = get_store()
    await store.ingest_documents(
        [
            {
                "url": "https://example.in/verified",
                "canonical_url": "https://example.in/verified",
                "domain": "example.in",
                "root_domain": "example.in",
                "domain_class": "static",
                "title": "Verified page about solar panels",
                "snippet": "Solar panels convert sunlight into electricity.",
                "content": "Solar panels convert sunlight into electricity.",
                "language": "en",
                "favicon_url": None,
                "payload_bytes": 150,
            }
        ]
    )

    async def _never(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("the index answered; discovery must not run")

    monkeypatch.setattr(live_retrieval, "retrieve", _never)
    service = search_service.SearchService(client=None, settings=Settings(turso_db_url="file:unused"))
    response = await service.search("solar panels", limit=5)

    assert response.source == "index"
    assert response.results[0].title == "Verified page about solar panels"
