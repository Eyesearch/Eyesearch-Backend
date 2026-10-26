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
    assert {"documents", "documents_fts", "crawl_backlog", "crawl_queue"} <= names

    fts = await client.execute("SELECT sql FROM sqlite_master WHERE name='documents_fts'")
    fts_sql = str((getattr(fts, "rows", ()) or [("",)])[0][0])
    assert "unicode61" in fts_sql
    assert "content='documents'" in fts_sql

    triggers = await client.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    trigger_names = {str(row[0]) for row in (getattr(triggers, "rows", ()) or ())}
    assert {"documents_ai", "documents_ad", "documents_au"} <= trigger_names

    columns = await client.execute("PRAGMA table_info(documents)")
    column_names = {str(row[1]) for row in (getattr(columns, "rows", ()) or ())}
    assert {"url", "title", "snippet", "content", "domain", "category", "created_at"} <= column_names


async def test_triggers_keep_the_index_in_sync(temp_db) -> None:
    client = temp_db
    await client.execute(
        "INSERT INTO documents (url, title, snippet, content, domain, category) VALUES (?, ?, ?, ?, ?, ?)",
        ["https://example.in/one", "Original title about solar panels", "s", "solar panels content", "example.in", "knowledge"],
    )

    async def matches(term: str) -> list[str]:
        result = await client.execute(
            "SELECT d.url FROM documents_fts JOIN documents d ON d.rowid = documents_fts.rowid "
            "WHERE documents_fts MATCH ?",
            [term],
        )
        return [str(row[0]) for row in (getattr(result, "rows", ()) or ())]

    assert await matches("solar") == ["https://example.in/one"]

    await client.execute(
        "UPDATE documents SET title = ?, content = ? WHERE url = ?",
        ["Updated title about wind turbines", "wind turbines content", "https://example.in/one"],
    )
    assert await matches("solar") == []          # the old text is gone from the index
    assert await matches("turbines") == ["https://example.in/one"]

    await client.execute("DELETE FROM documents WHERE url = ?", ["https://example.in/one"])
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
        "snippet": "first",
        "content": "first content about compilers",
        "language": "en",
        "favicon_url": None,
        "payload_bytes": 120,
    }

    first = await store.replace_documents([row])
    assert first["stored"] == 1

    second = await store.replace_documents([{**row, "title": "Version two of the page"}])
    assert second["stored"] == 1
    assert await store.count_documents() == 1

    stored = await store.get_document("https://example.in/seed")
    assert stored is not None
    assert stored["title"] == "Version two of the page"
    assert stored["category"] == "software"

    # The relevance gate applies to index hits too, so query what the title says.
    hits = await store.search_local("version page", limit=5)
    assert [hit.url for hit in hits] == ["https://example.in/seed"]


async def test_legacy_fts_shape_is_rebuilt(temp_db) -> None:
    client = temp_db
    await client.batch(
        [
            "DROP TRIGGER IF EXISTS documents_ai",
            "DROP TRIGGER IF EXISTS documents_ad",
            "DROP TRIGGER IF EXISTS documents_au",
            "DROP TABLE IF EXISTS documents_fts",
            "CREATE VIRTUAL TABLE documents_fts USING fts5(url, title, snippet, content, language, updated_at UNINDEXED)",
        ]
    )
    await client.execute(
        "INSERT INTO documents (url, title, snippet, content, domain) VALUES (?, ?, ?, ?, ?)",
        ["https://example.in/legacy", "Legacy row about metros", "s", "metro content", "example.in"],
    )

    await database.initialize_schema()  # rebuilds the index from documents

    fts = await client.execute("SELECT sql FROM sqlite_master WHERE name='documents_fts'")
    fts_sql = str((getattr(fts, "rows", ()) or [("",)])[0][0])
    assert "content='documents'" in fts_sql

    hits = await get_store().search_local("metros", limit=5)
    assert [hit.url for hit in hits] == ["https://example.in/legacy"]


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

    assert set(by_category) == {"government", "sports", "software", "knowledge"}
    assert all(len(urls) >= 3 for urls in by_category.values())

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

    monkeypatch.setattr(seed_crawler, "fetch_page", _fetch)

    settings = EngineSettings(
        respect_robots=False,
        crawl_delay_seconds=0.0,
        seed_crawl_pages_per_domain=1,
    )
    report = await seed_crawler.run_seed_pass(
        ["government"], settings=settings, store=get_store()
    )

    assert report["stored"] >= 1
    stored = await get_store().get_document("https://www.india.gov.in/")
    assert stored is not None
    assert stored["category"] == "government"
    assert len(str(stored["content"])) <= 1500
    assert str(stored["content"]).count(".") <= 4

    hits = await get_store().search_local("karnataka state portal", limit=5)
    assert hits, "the seed crawl should be searchable immediately"
