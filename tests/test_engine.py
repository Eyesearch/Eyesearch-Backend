"""Tests for the proprietary index: extraction, ranking, storage and routing."""

from __future__ import annotations

import asyncio

import pytest

from app import database
from app.config import Settings
from app.engine.config import EngineSettings
from app.engine.ranking import RankedDocument, freshness, host_trust, rank_documents
from app.engine.store import build_match_query
from app.engine.text import canonicalise, detect_language, extract_document
from app.services import live_retrieval, search_service
from app.services.live_retrieval import RawResult, RetrievalOutcome

ARTICLE_HTML = """
<html><head>
  <title>Namma Metro Purple Line timings — BMRC</title>
  <meta name="description" content="Purple Line services run from 05:00 to 23:00 on weekdays." />
  <link rel="canonical" href="https://english.bmrc.co.in/metro-timings?utm_source=feed" />
  <link rel="icon" href="/favicon.ico" />
</head><body>
  <nav>Home | About | Contact</nav>
  <article>
    <h1>Purple Line timings</h1>
    <p>The Purple Line runs between Whitefield and Challaghatta, with services
       starting at 05:00 and the last train at 23:00 on weekdays. Sunday services
       start later. Fares are distance based and can be paid by QR code.</p>
    <p>Baiyappanahalli and Majestic are the busiest interchanges on the network.</p>
  </article>
  <footer>© BMRC</footer>
</body></html>
"""

KANNADA_HTML = """
<html><head><title>ಕರ್ನಾಟಕ ಸರ್ಕಾರ</title>
<meta name="description" content="ಕರ್ನಾಟಕ ರಾಜ್ಯದ ಅಧಿಕೃತ ಜಾಲತಾಣ." /></head><body>
<article><p>ಕರ್ನಾಟಕ ರಾಜ್ಯದ ರಾಜಧಾನಿ ಬೆಂಗಳೂರು. ಇದು ದಕ್ಷಿಣ ಭಾರತದ ಪ್ರಮುಖ ನಗರವಾಗಿದೆ.
ಬೆಂಗಳೂರು ಮೆಟ್ರೋ, ನಮ್ಮ ಮೆಟ್ರೋ ಎಂದು ಜನಪ್ರಿಯವಾಗಿದೆ. ರಾಜ್ಯ ಸರ್ಕಾರದ ಎಲ್ಲಾ ಇಲಾಖೆಗಳ
ಮಾಹಿತಿ ಈ ಜಾಲತಾಣದಲ್ಲಿ ಲಭ್ಯವಿದೆ. ಕನ್ನಡ ಭಾಷೆಯಲ್ಲಿ ಮಾಹಿತಿ ನೀಡಲಾಗುತ್ತದೆ.</p></article>
</body></html>
"""


def test_extract_document_fields() -> None:
    doc = extract_document("https://english.bmrc.co.in/metro-timings?utm_source=x", ARTICLE_HTML)
    assert doc is not None
    assert doc.title == "Namma Metro Purple Line timings — BMRC"
    assert "Purple Line services run" in doc.snippet
    assert doc.url == "https://english.bmrc.co.in/metro-timings"
    assert doc.domain == "english.bmrc.co.in"
    assert doc.language == "en"
    assert doc.favicon_url.endswith("/favicon.ico")
    # navigation and footer text is not part of the body
    assert "Home | About" not in doc.content


def test_extract_document_detects_kannada() -> None:
    doc = extract_document("https://karnataka.gov.in/", KANNADA_HTML)
    assert doc is not None
    assert doc.language == "kn"


def test_extract_document_rejects_empty_pages() -> None:
    assert extract_document("https://example.in/", "<html><body>tiny</body></html>") is None


def test_canonicalise_strips_tracking_and_fragments() -> None:
    assert canonicalise("https://WWW.Example.in/path/?utm_source=a&b=2#top") == "https://example.in/path?b=2"


def test_detect_language_defaults_to_english() -> None:
    assert detect_language("bengaluru metro timings") == "en"


def test_build_match_query_is_safe() -> None:
    assert build_match_query("bengaluru metro") == '"bengaluru" "metro"*'
    assert build_match_query('"; DROP TABLE documents; --') == '"DROP" "TABLE" "documents"*'
    assert build_match_query("!!!") == ""


def test_ranking_prefers_fresh_trusted_documents() -> None:
    docs = [
        RankedDocument(url="https://blog.example.com/a", title="old", snippet="", language="en",
                       updated_at="2020-01-01 00:00:00", bm25=-5.0),
        RankedDocument(url="https://karnataka.gov.in/b", title="new", snippet="", language="en",
                       updated_at="2026-10-25 00:00:00", bm25=-5.0),
    ]
    ranked = rank_documents(docs)
    assert ranked[0].url == "https://karnataka.gov.in/b"
    assert ranked[0].score > ranked[1].score


def test_freshness_and_host_trust() -> None:
    assert freshness("2026-10-26 00:00:00") > 0.9
    assert freshness("2015-01-01 00:00:00") < 0.1
    assert host_trust("https://www.isro.gov.in/x") > host_trust("https://random.example/x")


@pytest.fixture()
async def temp_db(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/engine.db")
    await database.connect(settings)
    await database.initialize_schema()
    from app.engine.store import get_store

    await get_store().ensure_schema()
    yield
    await database.close()


async def _seed_index() -> None:
    from app.engine.store import get_store

    await get_store().ingest_documents(
        [
            {
                "url": "https://english.bmrc.co.in/metro-timings",
                "title": "Namma Metro Purple Line timings",
                "snippet": "Purple Line runs 05:00 to 23:00.",
                "content": "The Purple Line runs between Whitefield and Challaghatta. Metro timings for weekdays.",
                "language": "en",
                "domain": "english.bmrc.co.in",
            },
            {
                "url": "https://www.karnataka.gov.in/metro",
                "title": "Karnataka metro information",
                "snippet": "State metro information portal.",
                "content": "Metro projects across Karnataka, including Bengaluru metro timings.",
                "language": "en",
                "domain": "www.karnataka.gov.in",
            },
            {
                "url": "https://www.prajavani.net/metro",
                "title": "ಮೆಟ್ರೋ ಸುದ್ದಿ",
                "snippet": "ಮೆಟ್ರೋ ಸಂಚಾರ ಸಮಯ.",
                "content": "ಬೆಂಗಳೂರು ಮೆಟ್ರೋ ಸಂಚಾರ ಸಮಯ ಮತ್ತು ದರಗಳು.",
                "language": "kn",
                "domain": "www.prajavani.net",
            },
        ]
    )


async def test_local_search_returns_ranked_hits(temp_db) -> None:
    await _seed_index()
    from app.engine.store import get_store

    hits = await get_store().search_local("metro timings", limit=5)
    # The universal gate keeps hits whose title/URL/snippet covers the query:
    # the timings page does, the generic "metro information" page does not.
    assert hits
    assert all(hit.score > 0 for hit in hits)
    assert "timings" in hits[0].title.lower() or "timings" in hits[0].snippet.lower()

    kannada = await get_store().search_local("ಮೆಟ್ರೋ", limit=5)
    assert kannada and kannada[0].language == "kn"


async def test_search_service_answers_from_index(temp_db, monkeypatch) -> None:
    await _seed_index()
    called = {"live": 0}

    # Threshold is configuration, not a constant.
    monkeypatch.setattr(
        search_service,
        "get_engine_settings",
        lambda: EngineSettings(index_threshold=1),
    )

    async def _no_live(*args, **kwargs):
        called["live"] += 1
        return RetrievalOutcome(results=[])

    monkeypatch.setattr(live_retrieval, "retrieve", _no_live)
    service = search_service.SearchService(client=None, settings=Settings(turso_db_url="file:unused"))
    response = await service.search("metro timings", limit=5)

    assert response.source == "index"
    assert response.index_hits >= 1
    assert response.results and response.results[0].source == "eyesearch_index"
    assert called["live"] == 0


async def test_local_only_mode_queues_instead_of_calling_providers(temp_db, monkeypatch) -> None:
    """LIVE_SEARCH_ENABLED=false is the strictly-local deployment."""

    async def _never(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("local-only mode must not call providers")

    monkeypatch.setattr(live_retrieval, "retrieve", _never)
    monkeypatch.setattr(
        search_service, "get_engine_settings", lambda: EngineSettings(live_search_enabled=False)
    )
    service = search_service.SearchService(client=None, settings=Settings(turso_db_url="file:unused"))
    response = await service.search("something we have never crawled", limit=5)

    assert response.source == "queued"
    assert response.results == []

    await search_service.drain_background_writes()
    from app.engine.store import get_store

    assert "something we have never crawled" in await get_store().pending_backlog(10)


async def test_unverifiable_candidates_are_never_served(temp_db, monkeypatch) -> None:
    """Provider text is not served: candidates that fail the gate are dropped."""

    calls = {"live": 0}

    async def _unusable(query, limit, **kwargs):
        calls["live"] += 1
        # A repository loop and a deep application path: both rejected by is_indexable.
        return RetrievalOutcome(
            results=[
                RawResult(title="owner/repo", url="https://github.com/owner/repo", snippet="s", source="duckduckgo"),
                RawResult(title="deep", url="https://example.in/a/b/c/d/e", snippet="s", source="duckduckgo"),
            ],
            providers={"duckduckgo": 2},
        )

    monkeypatch.setattr(live_retrieval, "retrieve", _unusable)
    service = search_service.SearchService(client=None, settings=Settings(turso_db_url="file:unused"))
    response = await service.search("unindexed niche topic", limit=5)

    # Nothing verified means nothing served — the provider's raw text is not a result.
    assert response.source == "queued"
    assert response.results == []
    assert response.auto_ingested == 0

    await search_service.drain_background_writes()
    from app.engine.store import get_store

    assert "unindexed niche topic" in await get_store().pending_backlog(10)

    # A hopeless query is not retried against the providers straight away: the
    # repeat is served locally (result cache, or the backlog guard).
    repeat = await service.search("unindexed niche topic", limit=5)
    assert repeat.source in {"queued", "cache"}
    assert calls["live"] == 1


async def test_live_fetch_once_then_served_from_index(temp_db, monkeypatch) -> None:
    """The headline behaviour: one live call per topic, ever."""
    calls = {"live": 0}

    async def _live(query, limit, **kwargs):
        calls["live"] += 1
        return RetrievalOutcome(
            results=[
                RawResult(
                    title="Github — Wikipedia",
                    url="https://en.wikipedia.org/wiki/GitHub",
                    snippet="GitHub is a platform for version control and collaboration.",
                    source="duckduckgo",
                ),
                RawResult(
                    title="Github · GitHub",
                    url="https://github.com",
                    snippet="GitHub is where people build software.",
                    source="duckduckgo",
                ),
            ],
            providers={"duckduckgo": 2},
        )

    async def _extract(urls, *, client=None, settings=None, limit=None):
        from app.engine.text import ExtractedDocument
        from app.engine.urls import normalise_url

        documents = []
        for raw in urls:
            url = normalise_url(raw)
            title = "Github · GitHub" if "github.com" in url else "GitHub — Wikipedia"
            documents.append(
                ExtractedDocument(
                    url=url, canonical_url=url, domain=url.split("/")[2],
                    root_domain=url.split("/")[2], domain_class="static",
                    title=title, snippet="GitHub is where people build software.",
                    content=f"{title}. GitHub hosts repositories.",
                    language="en", favicon_url=None,
                )
            )
        return documents

    monkeypatch.setattr(live_retrieval, "retrieve", _live)
    monkeypatch.setattr(search_service, "extract_candidates", _extract)
    service = search_service.SearchService(client=None, settings=Settings(turso_db_url="file:unused"))

    first = await service.search("github", limit=5)
    assert first.source == "index", first.source
    assert first.results, "the first query must return the verified extracts"
    assert all(result.source == "eyesearch_index" for result in first.results)
    assert first.auto_ingested >= 1, "the verified documents must be written into the index"
    assert calls["live"] == 1

    second = await service.search("github", limit=5)
    assert second.source == "index", second.source
    assert second.index_hits >= 1
    assert calls["live"] == 1, "the second query must not touch the providers"
    assert second.results and second.results[0].source == "eyesearch_index"
