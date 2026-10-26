"""Autonomous Indian domain discovery, the backlog queue and the storage ceiling."""

from __future__ import annotations

import asyncio

import httpx

import pytest

from app import database
from app.config import Settings
from app.engine.config import EngineSettings
from app.engine.store import get_store
from app.indexer import crawler as seed_crawler
from app.indexer import discovery
from app.indexer.seed_registry import SEED_ROWS


@pytest.fixture()
async def temp_db(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/discovery.db")
    await database.connect(settings)
    await database.initialize_schema()
    yield
    await database.close()


def test_registry_has_150_plus_indian_roots() -> None:
    hosts = [row[0] for row in SEED_ROWS]
    assert len(hosts) >= 150
    assert len(set(hosts)) == len(hosts)

    categories = {row[2] for row in SEED_ROWS}
    assert {"government", "utilities", "education", "regulators", "services"} <= categories

    assert "maharashtra.gov.in" in hosts
    assert "tn.gov.in" in hosts
    assert "up.gov.in" in hosts
    assert "karnataka.gov.in" in hosts
    assert "sci.gov.in" in hosts
    assert "sebi.gov.in" in hosts
    assert "trai.gov.in" in hosts
    assert "digilocker.gov.in" in hosts
    assert "passportindia.gov.in" in hosts
    assert "gst.gov.in" in hosts
    assert "irctc.co.in" in hosts
    assert "cbse.gov.in" in hosts
    assert "cisce.org" in hosts


def test_indian_authority_filter() -> None:
    assert discovery.is_indian_authoritative("maharashtra.gov.in")
    assert discovery.is_indian_authoritative("iitb.ac.in")
    assert discovery.is_indian_authoritative("karnataka.gov.in")
    assert discovery.is_indian_authoritative("isro.gov.in")
    assert discovery.is_indian_authoritative("someuniversity.ac.in")

    assert not discovery.is_indian_authoritative("facebook.com")
    assert not discovery.is_indian_authoritative("cdn.jsdelivr.net")
    assert not discovery.is_indian_authoritative("bit.ly")
    assert not discovery.is_indian_authoritative("login.gov.in")
    assert not discovery.is_indian_authoritative("example.com")
    assert not discovery.is_indian_authoritative("192.168.1.1")


def test_discovery_keeps_indian_targets_and_drops_the_rest() -> None:
    html = """
    <a href="https://www.maharashtra.gov.in/">Maharashtra</a>
    <a href="https://facebook.com/xyz">Facebook</a>
    <a href="https://cdn.jsdelivr.net/lib.js">CDN</a>
    <a href="https://iitb.ac.in/about">IIT Bombay</a>
    <a href="https://bit.ly/abc">Shortener</a>
    <a href="/internal">Internal</a>
    <a href="https://www.google.com/search">Google</a>
    <a href="https://www.sebi.gov.in/">SEBI</a>
    <a href="https://example.com/">Not Indian</a>
    <a href="https://www.karnataka.gov.in/login">Login trap</a>
    <a href="https://karnataka.gov.in/inner">Same site</a>
    """
    hosts = discovery.discover_domains(html, "https://www.karnataka.gov.in/")
    assert hosts == ["maharashtra.gov.in", "iitb.ac.in", "sebi.gov.in"]


async def test_discovered_domains_are_queued_once_and_skipped_when_indexed(temp_db) -> None:
    store = get_store()
    html = '<a href="https://iitb.ac.in/">IIT</a><a href="https://sebi.gov.in/">SEBI</a>'

    added = await seed_crawler.enqueue_discovered(html, "https://www.karnataka.gov.in/")
    assert added == 2
    assert await store.pending_domain_count() == 2

    # Idempotent: the same links discovered again add nothing.
    assert await seed_crawler.enqueue_discovered(html, "https://www.karnataka.gov.in/") == 0

    # A host that is already indexed is never queued.
    await store.ingest_documents(
        [{"url": "https://iitb.ac.in/", "canonical_url": "https://iitb.ac.in/",
          "title": "IIT Bombay", "description": "Already indexed.", "payload_bytes": 100}]
    )
    await store.mark_domain_processed("iitb.ac.in")
    assert await seed_crawler.enqueue_discovered(
        '<a href="https://iitb.ac.in/x">IIT</a>', "https://www.karnataka.gov.in/"
    ) == 0


async def test_query_backlog_and_domain_backlog_stay_separate(temp_db) -> None:
    store = get_store()
    await store.enqueue_backlog("namma metro timings", 3)
    await store.enqueue_domain("maharashtra.gov.in")

    assert await store.pending_backlog(10) == ["namma metro timings"]
    assert await store.pending_domains(10) == ["maharashtra.gov.in"]
    assert await store.recently_queued("namma metro timings") is True
    assert await store.recently_queued("maharashtra.gov.in") is False


async def test_discovery_pass_crawls_in_batches_and_stores_sites(temp_db, monkeypatch) -> None:
    store = get_store()
    for host in ("maharashtra.gov.in", "tn.gov.in", "up.gov.in", "sebi.gov.in", "trai.gov.in"):
        await store.enqueue_domain(host)

    html = ("<html><head><title>State portal</title>"
            "<meta name='description' content='Official state portal.'></head>"
            "<body><nav><a href='/about'>About</a><a href='/contact'>Contact</a></nav>"
            "<article><p>This is an official Indian portal. It publishes notices. "
            "It lists services. It is authoritative.</p></article></body></html>")

    async def _fetch(client, url, settings, **kwargs):
        return html, 200

    monkeypatch.setattr(seed_crawler, "fetch_page", _fetch)
    settings = EngineSettings(
        seed_crawl_enabled=True,
        respect_robots=False,
        crawl_delay_seconds=0.0,
        seed_crawl_pages_per_domain=1,
        discovery_batch_size=2,
        discovery_batch_pause_seconds=0.0,
    )

    report = await seed_crawler.run_discovery_pass(store=store, settings=settings)

    assert report["attempted"] == 5
    assert report["batches"] == 3          # two, two, then one
    assert await store.count_documents() == 5
    assert await store.pending_domain_count() == 0


async def test_discovery_pass_respects_the_storage_ceiling(temp_db, monkeypatch) -> None:
    store = get_store()
    await store.enqueue_domain("maharashtra.gov.in")

    async def _never(*args, **kwargs):  # pragma: no cover - ceiling stops it first
        raise AssertionError("the ceiling is reached; nothing should be fetched")

    monkeypatch.setattr(seed_crawler, "fetch_page", _never)
    settings = EngineSettings(
        seed_crawl_enabled=True, respect_robots=False, crawl_delay_seconds=0.0,
        max_indexed_sites=0,
    )

    report = await seed_crawler.run_discovery_pass(store=store, settings=settings)

    assert report["ceiling"] is True
    assert report["attempted"] == 0
    assert await store.pending_domain_count() == 1  # left for later


# --- persistence: writes must not wait for the whole pass ------------------

async def test_each_domain_is_persisted_as_soon_as_it_finishes(temp_db, monkeypatch) -> None:
    """A long pass must not hold every write until the end (that lost work on restart)."""
    store = get_store()
    fast = seed_crawler.CategorySeed("https://fast.gov.in", "Fast", "government")
    slow = seed_crawler.CategorySeed("https://slow.gov.in", "Slow", "government")

    from app.engine.text import ExtractedDocument

    def _doc(seed, title):
        return ExtractedDocument(
            url=seed.url + "/", canonical_url=seed.url + "/", domain=seed.url.split("/")[2],
            root_domain=seed.url.split("/")[2], domain_class="static", title=title,
            snippet="Official portal.", content="Official portal content.",
            language="en", favicon_url=None, category="government",
        )

    async def _crawl(seed, *, client, settings, robots, max_pages=None):
        if seed is slow:
            await asyncio.sleep(1.0)
            return seed_crawler.SeedReport(category=seed.category, fetched=1, extracted=1), [_doc(seed, "Slow portal")]
        return seed_crawler.SeedReport(category=seed.category, fetched=1, extracted=1), [_doc(seed, "Fast portal")]

    monkeypatch.setattr(seed_crawler, "crawl_seed", _crawl)

    settings = EngineSettings(
        seed_crawl_enabled=True, respect_robots=False, crawl_delay_seconds=0.0,
        seed_crawl_pages_per_domain=1, seed_crawl_concurrency=2,
    )
    task = asyncio.create_task(
        seed_crawler.run_seed_pass(seeds=[fast, slow], settings=settings, store=store)
    )

    await asyncio.sleep(0.4)  # the slow seed is still crawling
    mid_pass = await store.count_documents()
    assert mid_pass == 1, "the finished domain should already be in the database"

    await task
    assert await store.count_documents() == 2


async def test_a_failed_write_does_not_kill_the_pass(temp_db, monkeypatch, caplog) -> None:
    store = get_store()
    seeds = [seed_crawler.CategorySeed(f"https://site{i}.gov.in", f"Site {i}", "government") for i in range(3)]

    from app.engine.text import ExtractedDocument

    async def _crawl(seed, *, client, settings, robots, max_pages=None):
        document = ExtractedDocument(
            url=seed.url + "/", canonical_url=seed.url + "/", domain=seed.url.split("/")[2],
            root_domain=seed.url.split("/")[2], domain_class="static", title="Portal",
            snippet="Snippet.", content="Content.", language="en", favicon_url=None,
            category="government",
        )
        return seed_crawler.SeedReport(category=seed.category, fetched=1, extracted=1), [document]

    monkeypatch.setattr(seed_crawler, "crawl_seed", _crawl)

    real_replace = store.replace_documents
    calls = {"n": 0}

    async def _flaky(documents):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("turso said no")
        return await real_replace(documents)

    monkeypatch.setattr(store, "replace_documents", _flaky)

    settings = EngineSettings(
        seed_crawl_enabled=True, respect_robots=False, crawl_delay_seconds=0.0,
        seed_crawl_concurrency=1,
    )
    with caplog.at_level("ERROR"):
        report = await seed_crawler.run_seed_pass(seeds=seeds, settings=settings, store=store)

    assert calls["n"] == 3
    assert await store.count_documents() == 2      # the other two persisted
    assert any("index write failed" in record.message for record in caplog.records)


async def test_successful_writes_are_logged_with_the_total(temp_db, monkeypatch, caplog) -> None:
    store = get_store()
    seed = seed_crawler.CategorySeed("https://logged.gov.in", "Logged", "government")

    from app.engine.text import ExtractedDocument

    async def _crawl(seed, *, client, settings, robots, max_pages=None):
        document = ExtractedDocument(
            url=seed.url + "/", canonical_url=seed.url + "/", domain=seed.url.split("/")[2],
            root_domain=seed.url.split("/")[2], domain_class="static", title="Logged portal",
            snippet="Snippet.", content="Content.", language="en", favicon_url=None,
            category="government",
        )
        return seed_crawler.SeedReport(category=seed.category, fetched=1, extracted=1), [document]

    monkeypatch.setattr(seed_crawler, "crawl_seed", _crawl)
    settings = EngineSettings(seed_crawl_enabled=True, respect_robots=False, crawl_delay_seconds=0.0)

    with caplog.at_level("INFO"):
        await seed_crawler.run_seed_pass(seeds=[seed], settings=settings, store=store)

    assert any(
        "DB COMMIT SUCCESS: Indexed logged.gov.in" in record.message for record in caplog.records
    )


# --- extraction gate: government homepages must not be silently dropped ----

def test_a_javascript_homepage_with_a_title_is_indexable() -> None:
    """The reported case: fetched, then "produced no indexable pages"."""
    from app.engine.text import extract_document_with_reason

    shell = (
        "<html><head><title>Government of Odisha</title>"
        "<meta name='description' content='Official portal of the Government of Odisha.'>"
        "</head><body><div id='app'></div><noscript>Enable JavaScript</noscript></body></html>"
    )
    document, reason = extract_document_with_reason("https://odisha.gov.in/", shell)

    assert document is not None, reason
    assert document.title == "Government of Odisha"
    assert document.description


def test_skip_reasons_are_specific() -> None:
    from app.engine.text import (
        SKIP_BLOCKED_STATUS,
        SKIP_NO_TITLE,
        SKIP_NOT_INDEXABLE,
        SKIP_TOO_SHORT,
        extract_document_with_reason,
    )

    page = "<html><head><title>Portal</title></head><body><p>Some text here.</p></body></html>"
    assert extract_document_with_reason("https://x.gov.in/", page, http_status=403)[1] == SKIP_BLOCKED_STATUS
    assert extract_document_with_reason("https://x.gov.in/", "")[1] == SKIP_TOO_SHORT
    assert extract_document_with_reason("https://x.gov.in/login", page)[1] == SKIP_NOT_INDEXABLE
    assert extract_document_with_reason(
        "https://x.gov.in/", "<html><body><div id='app'></div></body></html>"
    )[1] == SKIP_NO_TITLE


async def test_skip_reasons_are_logged_per_seed(temp_db, monkeypatch, caplog) -> None:
    store = get_store()
    seed = seed_crawler.CategorySeed("https://empty.gov.in", "Empty", "government")

    async def _fetch(client, url, settings, **kwargs):
        return "<html><body><div id='app'></div></body></html>", 200

    monkeypatch.setattr(seed_crawler, "fetch_page", _fetch)
    settings = EngineSettings(seed_crawl_enabled=True, respect_robots=False, crawl_delay_seconds=0.0)

    with caplog.at_level("INFO"):
        await seed_crawler.run_seed_pass(seeds=[seed], settings=settings, store=store)

    # The page is still reported as skipped, with the rule that rejected it…
    assert any("skipped: no_title" in record.message for record in caplog.records)
    # …but the seed's own root is committed from fallback metadata, so the domain
    # is never lost just because its entry page had nothing to extract.
    assert any("using fallback metadata" in record.message for record in caplog.records)
    assert any("DB COMMIT SUCCESS: Indexed empty.gov.in" in record.message for record in caplog.records)
    assert await store.count_documents() == 1


async def test_stats_endpoint_reads_live_counts(temp_db) -> None:
    from app.main import app

    store = get_store()
    await store.ingest_documents(
        [{"url": "https://live.gov.in/", "canonical_url": "https://live.gov.in/",
          "title": "Live portal", "description": "Snippet.", "payload_bytes": 100}]
    )
    await store.enqueue_domain("queued.gov.in")

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=10) as client:
            first = (await client.get("/v1/stats")).json()
            assert first["sites"] >= 1
            assert first["pages"] >= 1
            assert first["source"] == "database"
            assert first["discovered_backlog"] >= 1

            # A write shows up on the very next call: no cached count.
            await store.ingest_documents(
                [{"url": "https://second.gov.in/", "canonical_url": "https://second.gov.in/",
                  "title": "Second portal", "description": "Snippet.", "payload_bytes": 100}]
            )
            second = (await client.get("/v1/stats")).json()
            assert second["sites"] == first["sites"] + 1


# --- the root domain must persist even when extraction fails ---------------

async def test_a_fetched_seed_always_persists_a_root(temp_db, monkeypatch) -> None:
    """The PSPCL case: HTTP 200, no extractable content, domain still indexed."""
    store = get_store()
    seed = seed_crawler.CategorySeed(
        "https://www.pspcl.in", "PSPCL - Punjab State Power Corporation Limited", "utilities"
    )

    async def _fetch(client, url, settings, **kwargs):
        return "<html><body><div id='app'></div></body></html>", 200

    monkeypatch.setattr(seed_crawler, "fetch_page", _fetch)
    settings = EngineSettings(seed_crawl_enabled=True, respect_robots=False, crawl_delay_seconds=0.0)

    report = await seed_crawler.run_seed_pass(seeds=[seed], settings=settings, store=store)

    assert report["stored"] == 1
    assert await store.count_documents() == 1
    stored = await store.get_document("https://pspcl.in/")
    assert stored is not None
    assert "PSPCL" in stored["title"]
    assert stored["category"] == "utilities"


async def test_a_blocked_status_still_yields_no_root(temp_db, monkeypatch) -> None:
    """A 403 is not evidence of a real page; it must not be indexed."""
    store = get_store()
    seed = seed_crawler.CategorySeed("https://blocked.gov.in", "Blocked", "government")

    async def _fetch(client, url, settings, **kwargs):
        return "<html><body>Forbidden</body></html>", 403

    monkeypatch.setattr(seed_crawler, "fetch_page", _fetch)
    settings = EngineSettings(seed_crawl_enabled=True, respect_robots=False, crawl_delay_seconds=0.0)

    report = await seed_crawler.run_seed_pass(seeds=[seed], settings=settings, store=store)

    assert report["stored"] == 0
    assert await store.count_documents() == 0


# --- the write probe and step-level errors --------------------------------

async def test_startup_write_probe_confirms_a_writable_database(temp_db) -> None:
    from app import database

    report = await database.verify_database()

    assert report["writable"] is True
    assert report["counts"]["sites"] == 0
    # The probe row is cleaned up.
    assert await get_store().count_documents() == 0


async def test_startup_write_probe_fails_loudly_on_a_read_only_database(temp_db, monkeypatch, caplog) -> None:
    from app import database

    real_execute = database._execute

    async def _read_only(sql, params=None, *, timeout_s=None):
        if sql.strip().upper().startswith(("INSERT", "DELETE")):
            raise database.DatabaseError("SQLITE_READONLY: attempt to write a readonly database")
        return await real_execute(sql, params, timeout_s=timeout_s)

    monkeypatch.setattr(database, "_execute", _read_only)

    with caplog.at_level("ERROR"):
        report = await database.verify_database()

    assert report["writable"] is False
    assert "READONLY" in report["error"] or "readonly" in report["error"].lower()
    assert any("startup write check FAILED" in record.message for record in caplog.records)


async def test_a_step_level_error_inside_a_200_response_is_raised() -> None:
    """Turso answers 200 OK even when a statement failed inside the batch."""
    import httpx

    from app.errors import DatabaseError
    from app.libsql_http import TursoHTTPClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"type": "ok", "response": {"type": "execute", "result": {"cols": [], "rows": []}}},
                    {"type": "error", "error": {"message": "SQLITE_CONSTRAINT: UNIQUE constraint failed: sites.domain"}},
                ]
            },
        )

    client = TursoHTTPClient("https://db.turso.io", transport=httpx.MockTransport(handler))
    with pytest.raises(DatabaseError) as excinfo:
        await client.batch([("INSERT INTO sites (domain) VALUES (?)", ["a.in"]), "SELECT 1"])

    message = str(excinfo.value)
    assert "step 1 failed" in message
    assert "UNIQUE constraint" in message
    assert "statement:" in message
    await client.close()
