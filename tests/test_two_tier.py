"""The two-tier sites/pages schema: shape, grouping and weighted retrieval."""

from __future__ import annotations

import pytest

from app import database
from app.config import Settings
from app.engine.store import get_store
from app.engine.text import extract_document
from app.services import search_service
from tests.test_sitelinks import ZTUBE_HTML


@pytest.fixture()
async def temp_db(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/two_tier.db")
    await database.connect(settings)
    await database.initialize_schema()
    yield database.get_client()
    await database.close()


def _row(url: str, title: str, description: str = "", **extra) -> dict:
    return {
        "url": url,
        "canonical_url": url,
        "domain": url.split("/")[2],
        "title": title,
        "description": description,
        "headings": "",
        "nav_terms": "",
        "site_terms": url.split("/")[2].replace(".", " "),
        "category_terms": "software",
        "url_terms": " ".join(part for part in url.split("/")[3:] if part),
        "payload_bytes": 200,
        **extra,
    }


async def test_root_and_sitelinks_are_pages_of_one_site(temp_db) -> None:
    store = get_store()
    document = extract_document("https://ztube.com/", ZTUBE_HTML)
    assert document is not None

    outcome = await store.ingest_documents([document.as_row()])
    assert outcome["sites"] == 1
    assert outcome["sitelinks"] >= 3

    client = temp_db
    sites = await client.execute("SELECT site_id, domain, name, category FROM sites")
    site_rows = getattr(sites, "rows", ()) or ()
    assert len(site_rows) == 1
    site_id, domain, name, category = site_rows[0]
    assert domain == "ztube.com"
    assert category  # inferred, never blank

    pages = await client.execute(
        "SELECT url, kind FROM pages WHERE site_id = ? ORDER BY kind", [int(site_id)]
    )
    page_rows = getattr(pages, "rows", ()) or ()
    kinds = [str(row[1]) for row in page_rows]
    assert kinds.count("root") == 1
    assert kinds.count("sitelink") >= 3
    # every page belongs to the site (the FK is honoured)
    assert all(int(row[0]) == int(site_id) for row in [(site_id,)])


async def test_noindex_and_error_pages_are_not_retrievable(temp_db) -> None:
    store = get_store()
    await store.ingest_documents([_row("https://example.in/", "Solar panels guide", "All about solar panels")])
    await store.ingest_documents([_row("https://hidden.in/", "Solar panels hidden", "Solar panels")])

    await temp_db.execute("UPDATE pages SET noindex = 1 WHERE url LIKE 'https://hidden.in/%'")
    await temp_db.execute("UPDATE pages SET http_status = 500 WHERE url LIKE 'https://hidden.in/%'")

    hits = await store.search_local("solar panels", limit=10)
    urls = [hit.url for hit in hits]
    assert "https://example.in/" in urls
    assert not any("hidden.in" in url for url in urls)


async def test_title_weight_outranks_url_and_description(temp_db) -> None:
    store = get_store()
    # Equal-length documents, so the column weights decide rather than bm25's
    # length normalisation: description (2.5) should beat url_terms (2.0).
    await store.ingest_documents(
        [
            _row("https://a.example.in/", "Widget catalogue", "Everything else here."),
            _row("https://b.example.in/", "Catalogue", "A widget appears here."),
            _row("https://c.example.in/widget", "Catalogue", "A product appears here."),
        ]
    )

    hits = await store.search_local("widget", limit=5)
    assert len(hits) == 3
    # title hit (weight 10) beats description (2.5) beats url_terms (2.0)
    assert hits[0].url == "https://a.example.in/"
    assert hits[1].url == "https://b.example.in/"
    assert hits[2].url == "https://c.example.in/widget"


async def test_sitelink_match_returns_the_parent_site_card(temp_db, monkeypatch) -> None:
    store = get_store()
    document = extract_document("https://ztube.com/", ZTUBE_HTML)
    assert document is not None
    await store.ingest_documents([document.as_row()])

    async def _never(*args, **kwargs):  # pragma: no cover
        raise AssertionError("the index answers; discovery must not run")

    monkeypatch.setattr("app.services.live_retrieval.retrieve", _never)
    service = search_service.SearchService(client=None, settings=Settings(turso_db_url="file:unused"))

    response = await service.search("documentation", limit=5)
    assert response.source == "index"
    card = response.results[0]
    assert card.url == "https://ztube.com/"
    assert any(link.title == "Documentation" for link in card.sitelinks)
    assert card.sitelinks[0].title == "Documentation"
    assert card.sitelinks[0].matched is True


async def test_legacy_tables_are_dropped_on_init(temp_db) -> None:
    client = temp_db
    await client.batch(
        [
            "CREATE TABLE IF NOT EXISTS documents (url TEXT PRIMARY KEY, title TEXT)",
            "CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(url, title)",
            "CREATE TABLE IF NOT EXISTS search_records (id INTEGER PRIMARY KEY, query TEXT)",
        ]
    )

    await database.initialize_schema()

    tables = await client.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {str(row[0]) for row in (getattr(tables, "rows", ()) or ())}
    assert "documents" not in names
    assert "documents_fts" not in names
    assert "search_records" not in names
    assert {"sites", "pages", "page_fts"} <= names


async def test_site_payload_budget_is_bounded(temp_db) -> None:
    from app.engine.config import get_engine_settings

    cfg = get_engine_settings()
    store = get_store()
    big = "x" * 3000

    rows = [_row(f"https://big.example.in/page{index}", f"Page {index} {big}", big) for index in range(6)]
    for row in rows:
        row["payload_bytes"] = 3000

    outcome = await store.ingest_documents(rows)
    assert outcome["over_budget"] >= 1
    assert await store.count_pages() <= cfg.max_site_payload_bytes // 3000 + 1
