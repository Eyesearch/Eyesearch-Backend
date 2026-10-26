"""Regression tests for the relevance bug reported from production.

FTS5 matched a passing footer mention: "google" returned a cricket page. The
result cache that once amplified the damage is gone with the two-tier schema,
so only the matching behaviour is covered here.
"""

from __future__ import annotations

import pytest

from app import database
from app.config import Settings
from app.engine.config import EngineSettings
from app.engine.store import get_store, passes_relevance
from app.engine.ranking import RankedDocument
from app.services import live_retrieval, search_service
from app.services.live_retrieval import RawResult, RetrievalOutcome

RCB_TITLE = "Royal Challengers Bengaluru squad and fixtures"
RCB_URL = "https://www.royalchallengers.com/team"
MS_TITLE = "Phone Link pairing - microsoft.com"
MS_URL = "https://support.microsoft.com/windows/phone-link-pairing"
CRICKET_TITLE = "Delhi Capitals - iplt20.com"
CRICKET_URL = "https://www.iplt20.com/teams/delhi-capitals"


def _doc(title: str, url: str, snippet: str = "", content: str = "") -> dict:
    return {
        "url": url,
        "canonical_url": url,
        "domain": url.split("/")[2],
        "root_domain": url.split("/")[2],
        "domain_class": "static",
        "title": title,
        "snippet": snippet,
        "content": content or f"{title}. {snippet}",
        "language": "en",
        "favicon_url": None,
        "payload_bytes": 200,
    }


@pytest.fixture()
async def temp_db(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/relevance.db")
    await database.connect(settings)
    await database.initialize_schema()
    await get_store().ensure_schema()
    yield
    await database.close()


# --- 1. cache isolation ----------------------------------------------------

def test_relevance_rejects_a_body_only_match() -> None:
    settings = EngineSettings()
    weak = RankedDocument(
        url=CRICKET_URL,
        title=CRICKET_TITLE,
        snippet="Delhi Capitals squad, fixtures and results.",
        language="en",
        updated_at=None,
        bm25=-9.0,
    )
    assert passes_relevance("google", weak, settings) is False


def test_relevance_accepts_a_title_or_url_match() -> None:
    settings = EngineSettings()
    strong = RankedDocument(
        url="https://www.google.com/",
        title="Google",
        snippet="Search the world's information.",
        language="en",
        updated_at=None,
        bm25=-2.0,
    )
    assert passes_relevance("google", strong, settings) is True

    url_match = RankedDocument(
        url="https://www.google.com/",
        title="Search engine",
        snippet="",
        language="en",
        updated_at=None,
        bm25=-1.0,
    )
    assert passes_relevance("google", url_match, settings) is True


def test_relevance_needs_half_the_words_for_multiword_queries() -> None:
    settings = EngineSettings()
    partial = RankedDocument(
        url="https://english.bmrc.co.in/metro-timings",
        title="Namma Metro Purple Line",
        snippet="Purple Line timings and fares.",
        language="en",
        updated_at=None,
        bm25=-3.0,
    )
    assert passes_relevance("metro timings", partial, settings) is True
    # Only one of four words appears in a descriptive field: below the threshold.
    assert passes_relevance("metro bangalore traffic congestion", partial, settings) is False


async def test_google_does_not_return_a_cricket_page(temp_db) -> None:
    store = get_store()
    await store.ingest_documents(
        [
            _doc(
                CRICKET_TITLE,
                CRICKET_URL,
                snippet="Delhi Capitals squad and fixtures.",
                content="Delhi Capitals squad. Follow us on Google Play for updates.",
            ),
            _doc(
                "Google",
                "https://www.google.com/",
                snippet="Search the world's information, including webpages and images.",
            ),
        ]
    )

    hits = await store.search_local("google", limit=5)
    urls = [hit.url for hit in hits]
    # Documents are stored under their normalised URL (www. stripped).
    assert "https://google.com/" in urls
    assert "https://iplt20.com/teams/delhi-capitals" not in urls


async def test_rcb_returns_cricket_and_never_the_microsoft_page(temp_db) -> None:
    store = get_store()
    await store.ingest_documents(
        [
            _doc(RCB_TITLE, RCB_URL, snippet="RCB squad, fixtures and ticket news."),
            _doc(
                MS_TITLE,
                MS_URL,
                snippet="Pair your phone with your PC.",
                content="Phone Link pairing guide. RCB is not mentioned here, but Google is.",
            ),
        ]
    )

    hits = await store.search_local("RCB", limit=5)
    assert [hit.url for hit in hits] == ["https://royalchallengers.com/team"]


async def test_title_matches_outrank_snippet_matches(temp_db) -> None:
    store = get_store()
    await store.ingest_documents(
        [
            _doc("Metro timings explained", "https://a.example.in/metro", snippet="A guide."),
            _doc("City guide", "https://b.example.in/guide", snippet="Metro timings for the city."),
        ]
    )

    hits = await store.search_local("metro timings", limit=5)
    assert len(hits) == 2
    assert hits[0].url == "https://a.example.in/metro", [hit.url for hit in hits]


async def test_weak_index_hit_triggers_the_on_demand_pipeline(temp_db, monkeypatch) -> None:
    """A rejected match is a miss: discovery runs, and only verified pages are served."""
    store = get_store()
    await store.ingest_documents(
        [
            _doc(
                CRICKET_TITLE,
                CRICKET_URL,
                snippet="Delhi Capitals squad.",
                content="Delhi Capitals. Get the app on Google Play.",
            )
        ]
    )

    calls = {"live": 0}

    async def _live(query, limit, **kwargs):
        calls["live"] += 1
        return RetrievalOutcome(
            results=[
                RawResult(
                    title="Google",
                    url="https://www.google.com/",
                    snippet="Search the world's information.",
                    source="duckduckgo",
                )
            ],
            providers={"duckduckgo": 1},
        )

    async def _extract(urls, *, client=None, settings=None, limit=None):
        from app.engine.text import ExtractedDocument

        return [
            ExtractedDocument(
                url=url, canonical_url=url, domain=url.split("/")[2],
                root_domain=url.split("/")[2], domain_class="static",
                title="Google", snippet="Search the world's information.",
                content="Google. Search the world's information.",
                language="en", favicon_url=None,
            )
            for url in urls
            if "google.com" in url
        ]

    monkeypatch.setattr(live_retrieval, "retrieve", _live)
    monkeypatch.setattr(search_service, "extract_candidates", _extract)
    service = search_service.SearchService(client=None, settings=Settings(turso_db_url="file:unused"))
    response = await service.search("google", limit=5)

    assert calls["live"] == 1
    assert response.source == "index"
    assert "google.com" in response.results[0].url
    assert "iplt20.com" not in [result.url for result in response.results]
