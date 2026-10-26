"""Regression tests for the two bugs reported from production.

1. A query served another query's cached rows ("RCB" returning a Microsoft page
   with `source: cache`).
2. FTS5 matching a passing footer mention ("google" returning a cricket page).
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

def test_cache_key_is_a_hash_of_the_normalised_query() -> None:
    key = database.query_cache_key
    assert key("RCB") == key("  rcb  ") == key("rcb")
    assert key("rcb") != key("google")
    assert len(key("anything")) == 40
    assert all(character in "0123456789abcdef" for character in key("anything"))


async def test_a_query_never_sees_another_querys_cached_rows(temp_db) -> None:
    await database.upsert_records(
        "phone link pairing", [{"title": MS_TITLE, "url": MS_URL, "snippet": "Pair your phone."}]
    )
    await database.upsert_records(
        "rcb", [{"title": RCB_TITLE, "url": RCB_URL, "snippet": "RCB squad news."}]
    )

    rcb_rows = await database.fetch_cached("RCB", 10)
    assert [row["title"] for row in rcb_rows] == [RCB_TITLE]
    assert all("microsoft" not in (row["url"] or "") for row in rcb_rows)

    microsoft_rows = await database.fetch_cached("phone link pairing", 10)
    assert [row["title"] for row in microsoft_rows] == [MS_TITLE]


async def test_the_same_url_can_be_cached_under_two_queries(temp_db) -> None:
    shared = {"title": "Shared page", "url": "https://example.in/shared", "snippet": "s"}
    await database.upsert_records("alpha", [shared])
    await database.upsert_records("beta", [shared])

    assert await database.count_cached("alpha") == 1
    assert await database.count_cached("beta") == 1
    # Writing under "beta" must not move the row away from "alpha".
    assert [row["url"] for row in await database.fetch_cached("alpha", 10)] == ["https://example.in/shared"]


async def test_legacy_url_unique_table_is_rebuilt(temp_db) -> None:
    client = database.get_client()
    await client.execute("DROP TABLE IF EXISTS search_records")
    await client.execute(
        """
        CREATE TABLE search_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT,
            title TEXT,
            url TEXT UNIQUE,
            snippet TEXT,
            poster_url TEXT,
            source TEXT DEFAULT 'live_crawl',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await client.execute(
        "INSERT INTO search_records (query, title, url, snippet) VALUES (?, ?, ?, ?)",
        ["rcb", MS_TITLE, MS_URL, "poisoned row"],
    )

    await database.initialize_schema()

    info = await client.execute("PRAGMA table_info(search_records)")
    columns = {str(row[1]) for row in (getattr(info, "rows", ()) or ())}
    assert "query_key" in columns
    # The poisoned row is gone with the old table, so the bad cache cannot serve it.
    assert await database.fetch_cached("rcb", 10) == []


# --- 2. FTS relevance ------------------------------------------------------

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


async def test_weak_index_hit_triggers_the_live_fallback(temp_db, monkeypatch) -> None:
    """A rejected match is a miss: the first query falls back to live, then ingests."""
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

    monkeypatch.setattr(live_retrieval, "retrieve", _live)
    service = search_service.SearchService(client=None, settings=Settings(turso_db_url="file:unused"))
    response = await service.search("google", limit=5)

    assert calls["live"] == 1
    assert response.source == "live"
    assert response.results[0].url.startswith("https://www.google.com")
    assert CRICKET_URL not in [result.url for result in response.results]
