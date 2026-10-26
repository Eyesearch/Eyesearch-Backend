"""Domain root compaction and sitelink chunking."""

from __future__ import annotations

import pytest

from app import database
from app.config import Settings
from app.engine.store import domain_root_url, get_store, merge_sections
from app.engine.text import extract_document, extract_sections
from app.services import formatter, live_retrieval, search_service
from app.services.live_retrieval import RawResult, RetrievalOutcome

ZTUBE_HTML = """
<html><head><title>Ztube</title>
<meta name="description" content="Ztube is a video hosting platform." /></head>
<body>
<nav>
  <a href="/login">Sign in</a>
  <a href="/signup">Create account</a>
  <a href="/docs">Documentation</a>
  <a href="/pricing">Pricing</a>
  <a href="/dashboard">Dashboard</a>
  <a href="/support">Help</a>
</nav>
<article><p>Ztube is a video hosting platform. Creators upload videos.
Viewers watch and comment. Channels organise content.</p></article>
</body></html>
"""


@pytest.fixture()
async def temp_db(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/sitelinks.db")
    await database.connect(settings)
    await database.initialize_schema()
    yield
    await database.close()


def test_sections_are_extracted_and_capped() -> None:
    sections = extract_sections(ZTUBE_HTML, "https://ztube.com/")
    titles = [section["title"] for section in sections]

    assert titles[:4] == ["Sign in", "Sign up", "Documentation", "Pricing"]
    assert len(sections) <= 5                       # capped
    assert all(section["url"].startswith("https://ztube.com/") for section in sections)
    assert all(section["snippet"] for section in sections)


def test_document_payload_stays_under_one_and_a_half_kilobytes() -> None:
    document = extract_document("https://ztube.com/", ZTUBE_HTML)
    assert document is not None
    assert document.sections
    assert document.payload_bytes() <= 1536, document.payload_bytes()
    assert document.sections_json().startswith("[{")


async def test_one_root_row_per_domain_with_clean_sections(temp_db) -> None:
    store = get_store()
    root = extract_document("https://ztube.com/", ZTUBE_HTML)
    assert root is not None

    first = await store.ingest_documents([root.as_row()])
    assert first["inserted"] == 1

    # A sub-page of the same domain is folded into the root, not stored.
    sub = extract_document("https://ztube.com/pricing", ZTUBE_HTML)
    assert sub is not None
    second = await store.ingest_documents([sub.as_row()])

    assert second["collapsed"] == 1
    assert second["inserted"] == 0
    assert await store.count_documents() == 1

    stored = await store.get_document("https://ztube.com/")
    assert stored is not None
    assert int(stored["payload_bytes"]) <= 1536


def test_merge_sections_dedupes_and_caps() -> None:
    existing = [{"title": "Sign in", "url": "https://a.example.in/login", "snippet": "x"}]
    incoming = [
        {"title": "Sign in", "url": "https://a.example.in/login", "snippet": "duplicate"},
        {"title": "Pricing", "url": "https://a.example.in/pricing", "snippet": "y"},
    ]
    merged = merge_sections(existing, incoming, limit=5)
    assert [item["title"] for item in merged] == ["Sign in", "Pricing"]
    assert len(merge_sections(existing, incoming, limit=1)) == 1


def test_domain_root_url() -> None:
    assert domain_root_url("https://www.example.in/a/b") == "https://example.in/"
    assert domain_root_url("https://example.in") == "https://example.in/"


async def test_search_returns_sitelinks_for_a_domain_query(temp_db, monkeypatch) -> None:
    store = get_store()
    document = extract_document("https://ztube.com/", ZTUBE_HTML)
    assert document is not None
    await store.ingest_documents([document.as_row()])

    async def _never(*args, **kwargs):  # pragma: no cover - the index answers
        raise AssertionError("discovery must not run for an index hit")

    monkeypatch.setattr(live_retrieval, "retrieve", _never)
    service = search_service.SearchService(client=None, settings=Settings(turso_db_url="file:unused"))

    cards = await service.search("ztube", limit=5)
    assert cards.source == "index"
    card = cards.results[0]
    assert card.sitelinks, "the root card must carry its sections"
    assert [link.title for link in card.sitelinks][:2] == ["Sign in", "Sign up"]
    assert all(link.matched is False for link in card.sitelinks)


async def test_a_section_query_promotes_that_sitelink(temp_db, monkeypatch) -> None:
    store = get_store()
    document = extract_document("https://ztube.com/", ZTUBE_HTML)
    assert document is not None
    await store.ingest_documents([document.as_row()])

    async def _never(*args, **kwargs):  # pragma: no cover
        raise AssertionError("discovery must not run for an index hit")

    monkeypatch.setattr(live_retrieval, "retrieve", _never)
    service = search_service.SearchService(client=None, settings=Settings(turso_db_url="file:unused"))

    # "ztube pricing" — the Pricing section must lead the sub-cards.
    response = await service.search("ztube pricing", limit=5)
    assert response.source == "index"
    card = response.results[0]
    assert card.sitelinks[0].title == "Pricing"
    assert card.sitelinks[0].matched is True


def test_rank_sections_marks_only_the_matching_section() -> None:
    sections = [
        {"title": "Sign in", "url": "https://a.example.in/login", "snippet": "Access your account"},
        {"title": "Pricing", "url": "https://a.example.in/pricing", "snippet": "Plans and pricing"},
    ]
    ranked = formatter.rank_sections(sections, "a pricing")
    assert ranked[0].title == "Pricing"
    assert ranked[0].matched is True
    assert ranked[1].matched is False


async def test_meta_contract_carries_sitelinks(temp_db) -> None:
    store = get_store()
    document = extract_document("https://ztube.com/", ZTUBE_HTML)
    assert document is not None
    await store.ingest_documents([document.as_row()])

    service = search_service.SearchService(client=None, settings=Settings(turso_db_url="file:unused"))
    payload = await service.meta_search("ztube", limit=5)

    assert payload.results
    assert payload.results[0].sitelinks
    assert payload.results[0].sitelinks[0].url.startswith("https://ztube.com/")
