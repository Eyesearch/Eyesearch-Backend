"""Tests for the consumer protocols and the autonomous backlog worker."""

from __future__ import annotations

import json

import pytest

from app import database
from app.config import Settings
from app.engine.store import get_store
from app.engine.text import extract_document
from app.indexer import backlog_worker

DOC_HTML = """
<html><head><title>Bengaluru Metro Purple Line timings</title>
<meta name="description" content="Purple Line services run 05:00 to 23:00 on weekdays." />
<link rel="canonical" href="https://english.bmrc.co.in/metro-timings" />
<link rel="icon" href="/favicon.ico" /></head>
<body><article><p>The Purple Line runs between Whitefield and Challaghatta with services
starting at 05:00 and the last train at 23:00 on weekdays, fares paid by QR code.</p></article></body></html>
"""

WORKER_HTML = """
<html><head><title>KSRTC bus timetables for Karnataka</title>
<meta name="description" content="KSRTC publishes bus timetables for every depot in Karnataka." />
<link rel="canonical" href="https://www.ksrtc.in/timetables" /></head>
<body><article><p>KSRTC bus timetables cover every depot in Karnataka, with fares and
departure times published each quarter for long-distance and city routes.</p></article></body></html>
"""


@pytest.fixture()
async def seeded(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/protocols.db")
    await database.connect(settings)
    await database.initialize_schema()
    store = get_store()
    await store.ensure_schema()
    document = extract_document("https://english.bmrc.co.in/metro-timings", DOC_HTML)
    assert document is not None
    await store.ingest_documents([document.as_row()])
    yield store
    await database.close()


# --- 1. OpenSearch ---------------------------------------------------------

async def test_opensearch_description(client) -> None:
    response = await client.get("/opensearch.xml")
    assert response.status_code == 200
    assert "opensearchdescription+xml" in response.headers["content-type"]
    body = response.text
    assert "<ShortName>eyesearch</ShortName>" in body
    assert "{searchTerms}" in body
    # Both the human search URL and the suggestion URL are declared.
    assert 'type="text/html"' in body
    assert 'type="application/x-suggestions+json"' in body
    assert "/v1/suggest?q={searchTerms}" in body
    # The human search URL and the icon live with the web client, not the API.
    assert "eyesearch.github.io/Eyesearch/web/?q={searchTerms}" in body
    assert "eyesearch.github.io/Eyesearch/web/assets/icons/favicon.svg" in body


async def test_suggest_returns_opensearch_array(client, seeded) -> None:
    response = await client.get("/v1/suggest", params={"q": "bengaluru metro"})
    assert response.status_code == 200
    payload = json.loads(response.text)
    assert isinstance(payload, list) and len(payload) == 2
    assert payload[0] == "bengaluru metro"
    assert isinstance(payload[1], list)
    assert any("Metro" in title for title in payload[1]), payload[1]


async def test_suggest_is_empty_rather_than_an_error(client, seeded) -> None:
    blank = await client.get("/v1/suggest", params={"q": ""})
    assert blank.status_code == 200
    assert json.loads(blank.text) == ["", []]

    unknown = await client.get("/v1/suggest", params={"q": "zzzzzz"})
    assert unknown.status_code == 200
    assert json.loads(unknown.text)[1] == []


# --- 2. meta-search JSON ---------------------------------------------------

async def test_search_format_json_shape(client, seeded) -> None:
    response = await client.get("/v1/search", params={"q": "bengaluru metro", "format": "json"})
    assert response.status_code == 200
    body = response.json()

    assert set(body) >= {"query", "total_hits", "took_ms", "source", "results"}
    assert body["source"] == "eyesearch_index"
    assert body["total_hits"] >= 1
    assert isinstance(body["took_ms"], int)

    result = body["results"][0]
    assert set(result) >= {"title", "url", "snippet", "favicon"}
    assert result["url"].startswith("https://")
    assert result["favicon"].endswith("/favicon.ico")
    # The aggregator contract carries no presentation fields.
    assert "poster_url" not in result and "score" not in result


async def test_search_default_format_is_still_cards(client, seeded) -> None:
    response = await client.get("/v1/search", params={"q": "bengaluru metro"})
    body = response.json()
    assert "results" in body and "degraded" in body
    assert body["results"][0]["source"] == "eyesearch_index"


async def test_search_rejects_unknown_format(client) -> None:
    response = await client.get("/v1/search", params={"q": "x", "format": "yaml"})
    assert response.status_code == 400


# --- 3. RAG context endpoint ----------------------------------------------

async def test_retrieve_rag_shape(client, seeded) -> None:
    response = await client.get("/v1/retrieve", params={"q": "bengaluru metro", "max_results": 2})
    assert response.status_code == 200
    body = response.json()

    assert body["query"] == "bengaluru metro"
    assert body["source"] in {"index", "cache", "queued"}
    assert isinstance(body["context"], list) and body["context"]
    block = body["context"][0]
    assert set(block) == {"title", "url", "text"}
    assert "<" not in block["text"]  # no HTML in the context


async def test_retrieve_markdown_still_available(client, seeded) -> None:
    response = await client.get(
        "/v1/retrieve", params={"q": "bengaluru metro", "format": "markdown", "max_results": 2}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text.startswith("# bengaluru metro")


async def test_retrieve_max_results_is_respected(client, seeded) -> None:
    response = await client.get("/v1/retrieve", params={"q": "bengaluru metro", "max_results": 1})
    assert len(response.json()["context"]) == 1


# --- 4. autonomous backlog worker -----------------------------------------

async def test_backlog_worker_resolves_a_missed_query(seeded, monkeypatch) -> None:
    store = seeded
    await store.enqueue_backlog("ksrtc bus timetables", 0)
    assert "ksrtc bus timetables" in await store.pending_backlog(5)

    async def _discover(query, settings, client):
        return ["https://www.ksrtc.in/timetables"]

    async def _fetch(client, url, settings, **kwargs):
        return WORKER_HTML, 200

    monkeypatch.setattr(backlog_worker, "discover_urls", _discover)
    monkeypatch.setattr(backlog_worker, "fetch_page", _fetch)

    before = await store.count_documents()
    report = await backlog_worker.run_backlog_once(limit=3)

    assert report["queries"] == 1
    assert report["resolved"] == 1
    assert report["inserted"] == 1
    assert await store.count_documents() == before + 1

    # The query is resolved, so it leaves the pending backlog.
    assert "ksrtc bus timetables" not in await store.pending_backlog(5)

    # …and the same query now hits the index.
    hits = await store.search_local("ksrtc bus timetables", limit=5)
    # Stored under the normalised URL (www. stripped) — the dedupe key.
    assert hits and hits[0].url == "https://ksrtc.in/timetables"


async def test_backlog_worker_leaves_unresolved_queries_pending(seeded, monkeypatch) -> None:
    store = seeded
    await store.enqueue_backlog("nothing crawlable here", 0)

    async def _no_urls(query, settings, client):
        return []

    monkeypatch.setattr(backlog_worker, "discover_urls", _no_urls)
    report = await backlog_worker.run_backlog_once(limit=3)

    assert report["resolved"] == 0
    assert "nothing crawlable here" in await store.pending_backlog(5)
