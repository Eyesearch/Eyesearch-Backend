"""Schema recreation, FTS sync triggers and the category-seed crawler."""

from __future__ import annotations

import pytest

from app import database
from app.config import Settings
from app.engine.config import EngineSettings
from app.engine.store import get_store
from app.engine.text import summarise
from app.engine.urls import is_indexable, is_wiki_article
from app.indexer import crawler as seed_crawler


@pytest.fixture()
async def temp_db(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/schema.db")
    await database.connect(settings)
    await database.initialize_schema()
    yield database.get_client()
    await database.close()


# --- 1. clean-start schema -------------------------------------------------

async def test_clean_start_creates_every_table_and_trigger(temp_db) -> None:
    client = temp_db

    tables = await client.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {str(row[0]) for row in (getattr(tables, "rows", ()) or ())}
    assert {"sites", "pages", "page_fts", "crawl_backlog", "crawl_queue"} <= names
    # the legacy flat model is gone
    assert "documents" not in names
    assert "documents_fts" not in names
    assert "search_records" not in names

    fts = await client.execute("SELECT sql FROM sqlite_master WHERE name='page_fts'")
    fts_sql = str((getattr(fts, "rows", ()) or [("",)])[0][0])
    assert "unicode61" in fts_sql
    assert "content='pages'" in fts_sql
    assert "detail=full" in fts_sql

    triggers = await client.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    trigger_names = {str(row[0]) for row in (getattr(triggers, "rows", ()) or ())}
    assert {"pages_ai", "pages_ad", "pages_au"} <= trigger_names

    page_columns = await client.execute("PRAGMA table_info(pages)")
    page_names = {str(row[1]) for row in (getattr(page_columns, "rows", ()) or ())}
    assert {
        "page_id", "site_id", "url", "kind", "title", "headings", "description",
        "nav_terms", "site_terms", "category_terms", "url_terms", "http_status", "noindex",
    } <= page_names

    site_columns = await client.execute("PRAGMA table_info(sites)")
    site_names = {str(row[1]) for row in (getattr(site_columns, "rows", ()) or ())}
    assert {"site_id", "domain", "canonical_url", "name", "category", "authority_score"} <= site_names


async def test_triggers_keep_the_index_in_sync(temp_db) -> None:
    client = temp_db
    await client.execute(
        "INSERT INTO sites (domain, canonical_url, name, category) VALUES (?, ?, ?, ?)",
        ["example.in", "https://example.in/", "Example", "knowledge"],
    )
    site = await client.execute("SELECT site_id FROM sites WHERE domain = ?", ["example.in"])
    site_id = int((getattr(site, "rows", ()) or [(0,)])[0][0])

    await client.execute(
        "INSERT INTO pages (site_id, url, kind, title, description) VALUES (?, ?, ?, ?, ?)",
        [site_id, "https://example.in/one", "root", "Original title about solar panels", "solar panels content"],
    )

    async def matches(term: str) -> list[str]:
        result = await client.execute(
            "SELECT p.url FROM page_fts JOIN pages p ON p.page_id = page_fts.rowid "
            "WHERE page_fts MATCH ?",
            [term],
        )
        return [str(row[0]) for row in (getattr(result, "rows", ()) or ())]

    assert await matches("solar") == ["https://example.in/one"]

    await client.execute(
        "UPDATE pages SET title = ?, description = ? WHERE url = ?",
        ["Updated title about wind turbines", "wind turbines content", "https://example.in/one"],
    )
    assert await matches("solar") == []
    assert await matches("turbines") == ["https://example.in/one"]

    await client.execute("DELETE FROM pages WHERE url = ?", ["https://example.in/one"])
    assert await matches("turbines") == []


async def test_insert_or_replace_keeps_one_row_and_updates_the_index(temp_db) -> None:
    store = get_store()
    row = {
        "url": "https://example.in/seed",
        "canonical_url": "https://example.in/seed",
        "domain": "example.in",
        "domain_class": "static",
        "category": "software",
        "title": "Version one of the page",
        "description": "first description about compilers",
        "headings": "Compilers",
        "nav_terms": "Docs Pricing",
        "site_terms": "example in",
        "category_terms": "software",
        "url_terms": "seed",
        "payload_bytes": 120,
    }

    first = await store.replace_documents([row])
    assert first["pages"] == 1

    second = await store.replace_documents([{**row, "title": "Version two of the page"}])
    assert second["pages"] == 1
    assert await store.count_pages() == 1
    assert await store.count_documents() == 1

    stored = await store.get_document("https://example.in/seed")
    assert stored is not None
    assert stored["title"] == "Version two of the page"

    hits = await store.search_local("version page", limit=5)
    assert [hit.url for hit in hits] == ["https://example.in/seed"]


async def test_legacy_page_fts_shape_is_rebuilt(temp_db) -> None:
    client = temp_db
    await client.batch(
        [
            "DROP TRIGGER IF EXISTS pages_ai",
            "DROP TRIGGER IF EXISTS pages_ad",
            "DROP TRIGGER IF EXISTS pages_au",
            "DROP TABLE IF EXISTS page_fts",
            "CREATE VIRTUAL TABLE page_fts USING fts5(url, title, description)",
        ]
    )

    await database.initialize_schema()  # rebuilds the index from pages

    fts = await client.execute("SELECT sql FROM sqlite_master WHERE name='page_fts'")
    fts_sql = str((getattr(fts, "rows", ()) or [("",)])[0][0])
    assert "content='pages'" in fts_sql


# --- 2. crawl scope --------------------------------------------------------

def test_wiki_articles_allowed_and_metadata_pages_rejected() -> None:
    assert is_wiki_article("https://en.wikipedia.org/wiki/Karnataka") is True
    assert is_indexable("https://en.wikipedia.org/wiki/Karnataka") is True

    assert is_wiki_article("https://en.wikipedia.org/wiki/Talk:Karnataka") is False
    assert is_indexable("https://en.wikipedia.org/wiki/Talk:Karnataka") is False
    assert is_indexable("https://en.wikipedia.org/wiki/Special:Search") is False
    assert is_indexable("https://en.wikipedia.org/wiki/User:Someone") is False

    # Level-1 only elsewhere.
    assert is_indexable("https://www.karnataka.gov.in/transport") is True
    assert is_indexable("https://www.karnataka.gov.in/a/b") is False


def test_summary_is_three_to_four_sentences() -> None:
    body = " ".join(f"Sentence number {index} about Karnataka." for index in range(1, 12))
    summary = summarise(body, max_sentences=4, max_chars=1500)

    assert summary.count(".") == 4
    assert summary.endswith(".")
    assert len(summary) <= 1500
    assert "Sentence number 5" not in summary

    long_sentences = " ".join(["A very long factual sentence about the state."] * 40)
    assert len(summarise(long_sentences, max_sentences=4, max_chars=300)) <= 300


# --- 3. category seeds and a crawl pass -----------------------------------

def test_category_seeds_cover_every_category() -> None:
    by_category: dict[str, list[str]] = {}
    for seed in seed_crawler.CATEGORY_SEEDS:
        by_category.setdefault(seed.category, []).append(seed.url)

    # The registry covers every Indian category the brief names.
    assert {"government", "utilities", "education", "regulators", "services",
            "banking", "sports", "ecommerce"} <= set(by_category)
    assert len(seed_crawler.CATEGORY_SEEDS) >= 150
    assert all(len(urls) >= 3 for urls in by_category.values())
    assert any("rbi.org.in" in url for url in by_category["banking"])
    assert any("flipkart.com" in url for url in by_category["ecommerce"])
    assert any("iitb.ac.in" in url for url in by_category["education"])
    assert any("sci.gov.in" in url for url in by_category["regulators"])
    assert any("digilocker.gov.in" in url for url in by_category["services"])
    assert any("bescom.co.in" in url for url in by_category["utilities"])

    assert any("iplt20.com" in url for url in by_category["sports"])
    assert any("bcci.tv" in url for url in by_category["sports"])
    assert any("prokabaddi.com" in url for url in by_category["sports"])
    assert any("github.com" in url for url in by_category["software"])
    assert any("python.org" in url for url in by_category["software"])
    assert any("developer.mozilla.org" in url for url in by_category["software"])
    assert all("/wiki/" in url for url in by_category["knowledge"])


async def test_seed_pass_stores_documents_with_their_category(temp_db, monkeypatch) -> None:
    html = """
    <html><head><title>Karnataka state portal</title>
    <meta name="description" content="Official portal of the Government of Karnataka." /></head>
    <body><article><p>Karnataka is a state in southern India. Its capital is Bengaluru.
    The state has 31 districts. Kannada is the official language. The state was formed in 1956.</p>
    </article></body></html>
    """

    async def _fetch(client, url, settings, **kwargs):
        return html, 200

    monkeypatch.setattr(seed_crawler, "fetch_page_status", _fetch)
    # Three seeds are enough to prove the pass; the registry has 200+.
    monkeypatch.setattr(
        seed_crawler, "seeds_for", lambda categories=None: seed_crawler.CATEGORY_SEEDS[:3]
    )

    settings = EngineSettings(
        seed_crawl_enabled=True,
        respect_robots=False,
        crawl_delay_seconds=0.0,
        seed_crawl_pages_per_domain=1,
    )
    report = await seed_crawler.run_seed_pass(
        ["government"], settings=settings, store=get_store()
    )

    assert report["stored"] >= 1
    stored = await get_store().get_document("https://india.gov.in/")
    assert stored is not None
    assert stored["category"] == "government"
    assert len(str(stored["description"])) <= 500

    hits = await get_store().search_local("karnataka state portal", limit=5)
    assert hits, "the seed crawl should be searchable immediately"
