"""Tests for the storage budget, deduplication, path filtering and revalidation."""

from __future__ import annotations

import pytest

from app import database
from app.config import Settings
from app.engine.store import get_store
from app.engine.text import extract_document
from app.engine.urls import is_indexable, normalise_url, root_domain
from app.indexer import revalidate as revalidate_module
from app.indexer.revalidate import revalidate_batch

# A page far larger than the budget: long paragraphs, scripts, styles, an inline
# base64 image and a tracking pixel. The stored row must still be ≤ 2 KB.
HUGE_HTML = """
<html><head>
  <title>A very long government portal page about Karnataka transport services and timetables</title>
  <meta name="description" content="Karnataka transport portal: bus and metro timetables, fares and service updates for Bengaluru and the rest of the state." />
  <link rel="canonical" href="https://www.karnataka.gov.in/transport?utm_source=newsletter&utm_medium=email" />
  <link rel="icon" href="/static/favicon.ico" />
  <style>body { background: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=='); }</style>
  <script>var tracking = "AAAA"; function big() { return "%s"; }</script>
</head><body>
  <nav>Home | Departments | Contact | Login</nav>
  <img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7" />
  <img src="https://tracker.example.com/pixel.gif?uid=12345" width="1" height="1" />
  <article><p>%s</p><p>%s</p><p>%s</p></article>
  <footer>Copyright Government of Karnataka</footer>
</body></html>
""" % ("x" * 5000, "Karnataka transport services cover buses and metro. " * 60,
       "Fares depend on distance and can be paid by QR code. " * 60,
       "Timetables are published every quarter for every depot. " * 60)

ENTRY_HTML = """
<html><head><title>Karnataka Transport Portal</title>
<meta name="description" content="Bus and metro timetables for Karnataka." />
<link rel="canonical" href="https://www.karnataka.gov.in/transport" /></head>
<body><article><p>Karnataka transport services cover buses and metro across the state,
with timetables published for every depot and fares paid by QR code at the gate.</p></article></body></html>
"""


@pytest.fixture()
async def temp_db(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/budget.db")
    await database.connect(settings)
    await database.initialize_schema()
    await get_store().ensure_schema()
    yield
    await database.close()


# --- 1. storage footprint ---------------------------------------------------

def test_extracted_payload_fits_two_kilobytes() -> None:
    document = extract_document("https://www.karnataka.gov.in/transport", HUGE_HTML)
    assert document is not None
    assert document.payload_bytes() <= 2048, document.payload_bytes()
    assert len(document.title) <= 100
    assert len(document.snippet) <= 200
    # The deep paragraph dump is gone, and so is the inline image data.
    assert len(document.content) <= 1200
    assert "base64" not in document.content.lower()
    assert "iVBORw0KGgo" not in document.content
    assert "Home | Departments" not in document.content


async def test_stored_row_fits_two_kilobytes(temp_db) -> None:
    document = extract_document("https://www.karnataka.gov.in/transport", HUGE_HTML)
    assert document is not None
    await get_store().ingest_documents([document.as_row()])

    stored = await get_store().get_document("https://www.karnataka.gov.in/transport")
    assert stored is not None
    assert int(stored["payload_bytes"]) <= 2048
    assert len(str(stored["title"])) <= 100
    assert len(str(stored["snippet"])) <= 200
    assert await get_store().max_payload_bytes() <= 2048


# --- 2. canonical deduplication --------------------------------------------

def test_normalise_url_strips_tracking_and_fragments() -> None:
    assert normalise_url("https://WWW.Karnataka.gov.in/transport/?utm_source=x&utm_medium=y#top") == (
        "https://karnataka.gov.in/transport"
    )
    assert normalise_url("https://example.in/a?ref=feed&b=2") == "https://example.in/a?b=2"
    assert root_domain("https://www.karnataka.gov.in/transport") == "karnataka.gov.in"


async def test_duplicate_normalised_urls_are_rejected(temp_db) -> None:
    first = extract_document("https://www.karnataka.gov.in/transport", ENTRY_HTML)
    assert first is not None
    outcome = await get_store().ingest_documents([first.as_row()])
    assert outcome["inserted"] == 1

    # Same page reached through a tracking link: same normalised key.
    again = extract_document("https://www.karnataka.gov.in/transport?utm_source=newsletter", ENTRY_HTML)
    assert again is not None
    second = await get_store().ingest_documents([again.as_row()])

    assert second["duplicate"] == 1
    assert second["inserted"] == 0
    assert await get_store().count_documents() == 1


async def test_domain_cap_is_enforced(temp_db) -> None:
    from app.engine.config import EngineSettings, get_engine_settings

    cap = get_engine_settings().max_urls_per_domain
    rows = []
    for index in range(cap + 3):
        document = extract_document(f"https://www.karnataka.gov.in/page{index}", ENTRY_HTML)
        assert document is not None
        rows.append(document.as_row())

    outcome = await get_store().ingest_documents(rows)
    assert outcome["inserted"] == cap
    assert outcome["domain_cap"] == 3
    assert await get_store().count_documents() == cap


# --- 3. deep path filtering ------------------------------------------------

def test_deep_paths_and_loops_are_filtered() -> None:
    assert is_indexable("https://www.karnataka.gov.in/transport") is True
    assert is_indexable("https://www.karnataka.gov.in") is True

    # Deep application paths.
    assert is_indexable("https://www.karnataka.gov.in/a/b/c/d") is False
    # Repository loops.
    assert is_indexable("https://github.com/owner/repo/tree/main/app") is False
    assert is_indexable("https://github.com/owner/repo") is False
    assert is_indexable("https://github.com/owner/repo/commit/abc123") is False
    # Pagination and dynamic chains.
    assert is_indexable("https://www.thehindu.com/news/page/2") is False
    assert is_indexable("https://www.example.in/list?page=3") is False
    assert is_indexable("https://www.example.in/list?a=1&b=2&c=3&d=4") is False
    # Non-documents.
    assert is_indexable("https://www.example.in/poster.jpg") is False


def test_deep_page_is_not_extracted() -> None:
    assert extract_document("https://www.karnataka.gov.in/a/b/c/d", ENTRY_HTML) is None


# --- 4. revalidation updates in place --------------------------------------

CHANGED_HTML = ENTRY_HTML.replace(
    "<title>Karnataka Transport Portal</title>",
    "<title>Karnataka Transport Portal — updated timetables</title>",
)


async def test_revalidation_updates_in_place(temp_db, monkeypatch) -> None:
    document = extract_document("https://www.karnataka.gov.in/transport", ENTRY_HTML)
    assert document is not None
    await get_store().ingest_documents([document.as_row()])

    async def _changed(client, url, settings, **kwargs):
        return CHANGED_HTML, 200

    monkeypatch.setattr(revalidate_module, "fetch_page", _changed)
    report = await revalidate_batch(limit=5)

    assert report["updated"] == 1
    assert await get_store().count_documents() == 1  # no duplicate row

    stored = await get_store().get_document("https://www.karnataka.gov.in/transport")
    assert stored is not None
    assert "updated timetables" in str(stored["title"])
    assert stored["last_checked_at"] is not None


async def test_revalidation_unchanged_writes_nothing(temp_db, monkeypatch) -> None:
    document = extract_document("https://www.karnataka.gov.in/transport", ENTRY_HTML)
    assert document is not None
    await get_store().ingest_documents([document.as_row()])
    before = await get_store().get_document("https://www.karnataka.gov.in/transport")

    async def _not_modified(client, url, settings, **kwargs):
        return None  # 304

    monkeypatch.setattr(revalidate_module, "fetch_page", _not_modified)
    report = await revalidate_batch(limit=5)

    assert report["unchanged"] == 1
    assert report["updated"] == 0

    after = await get_store().get_document("https://www.karnataka.gov.in/transport")
    assert after == before  # zero writes, including last_checked_at
