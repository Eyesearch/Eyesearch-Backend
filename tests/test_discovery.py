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

    monkeypatch.setattr(seed_crawler, "fetch_page_status", _fetch)
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

    monkeypatch.setattr(seed_crawler, "fetch_page_status", _never)
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
    assert any("index write failed" in record.getMessage() for record in caplog.records)


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
        "DB COMMIT SUCCESS: Indexed logged.gov.in" in record.getMessage() for record in caplog.records
    )


# --- extraction gate: government homepages must not be silently dropped ----

def test_a_javascript_shell_is_refused_by_the_gate() -> None:
    """No server-rendered text means no document, whatever the title says."""
    from app.engine.text import SKIP_THIN, extract_document_with_reason

    shell = (
        "<html><head><title>Government of Odisha</title>"
        "<meta name='description' content='Official portal of the Government of Odisha.'>"
        "</head><body><div id='app'></div><noscript>Enable JavaScript</noscript></body></html>"
    )
    document, reason = extract_document_with_reason("https://odisha.gov.in/", shell)

    assert document is None
    assert reason == SKIP_THIN


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
        # A shell: title and description present, no server-rendered text.
        return ("<html><head><title>Empty portal</title>"
                "<meta name='description' content='Official portal with no rendered text.'>"
                "</head><body><div id='app'></div></body></html>", 200)

    monkeypatch.setattr(seed_crawler, "fetch_page_status", _fetch)
    settings = EngineSettings(seed_crawl_enabled=True, respect_robots=False, crawl_delay_seconds=0.0)

    with caplog.at_level("INFO"):
        report = await seed_crawler.run_seed_pass(seeds=[seed], settings=settings, store=store)

    # The reason is recorded against the seed, and the whitelisted .gov.in seed
    # keeps a root card instead of being lost.
    reasons = report["categories"]["Empty"]["reasons"]
    assert reasons.get("low_text_density") == 1
    assert any("keeping a root card for empty.gov.in" in r.getMessage() for r in caplog.records)
    assert any("DB COMMIT SUCCESS: Indexed empty.gov.in" in r.getMessage() for r in caplog.records)
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

async def test_a_whitelisted_seed_persists_a_root_without_content(temp_db, monkeypatch) -> None:
    """A .gov.in/.nic.in portal that answers 200 with nothing extractable."""
    store = get_store()
    seed = seed_crawler.CategorySeed(
        "https://www.pspcl.gov.in", "PSPCL - Punjab State Power Corporation Limited", "utilities"
    )

    async def _fetch(client, url, settings, **kwargs):
        return "<html><body><div id='app'></div></body></html>", 200

    monkeypatch.setattr(seed_crawler, "fetch_page_status", _fetch)
    settings = EngineSettings(seed_crawl_enabled=True, respect_robots=False, crawl_delay_seconds=0.0)

    report = await seed_crawler.run_seed_pass(seeds=[seed], settings=settings, store=store)

    assert report["stored"] == 1
    assert await store.count_documents() == 1
    stored = await store.get_document("https://pspcl.gov.in/")
    assert stored is not None
    assert "PSPCL" in stored["title"]
    assert stored["category"] == "utilities"


async def test_a_whitelisted_seed_survives_a_403(temp_db, monkeypatch) -> None:
    """A government portal that refuses us keeps a root card."""
    store = get_store()
    seed = seed_crawler.CategorySeed("https://eci.gov.in", "Election Commission of India", "government")

    async def _fetch(client, url, settings, **kwargs):
        return None, 403

    monkeypatch.setattr(seed_crawler, "fetch_page_status", _fetch)
    settings = EngineSettings(seed_crawl_enabled=True, respect_robots=False, crawl_delay_seconds=0.0)

    report = await seed_crawler.run_seed_pass(seeds=[seed], settings=settings, store=store)

    assert report["stored"] == 1
    assert await store.count_documents() == 1
    stored = await store.get_document("https://eci.gov.in/")
    assert stored is not None and stored["title"] == "eci.gov.in"


async def test_a_5xx_or_ssl_failure_still_persists_the_hostname_root(temp_db, monkeypatch) -> None:
    """Only invalid syntax is skipped: a refused domain is still a real domain."""
    store = get_store()
    cases = [
        ("https://broken.gov.in", None, 502),
        ("https://sslbroken.nic.in", None, None),   # connection / SSL failure
        ("https://eci.gov.in", None, 403),
    ]

    async def _fetch(client, url, settings, **kwargs):
        for prefix, html, status in cases:
            if url.startswith(prefix):
                return html, status
        return None, 406

    monkeypatch.setattr(seed_crawler, "fetch_page_status", _fetch)
    settings = EngineSettings(seed_crawl_enabled=True, respect_robots=False, crawl_delay_seconds=0.0)
    seeds = [seed_crawler.CategorySeed(prefix, "Seed", "government") for prefix, _, _ in cases]

    report = await seed_crawler.run_seed_pass(seeds=seeds, settings=settings, store=store)

    assert report["stored"] == 3
    assert await store.count_documents() == 3
    for prefix, _, _ in cases:
        host = prefix.split("//")[1]
        stored = await store.get_document(f"https://{host}/")
        assert stored is not None, host
        assert stored["title"] == host


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
    assert any("startup write check FAILED" in record.getMessage() for record in caplog.records)


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


# --- instrumentation: prove what is actually being executed ---------------

async def test_pipeline_statements_are_logged_with_their_verbs(caplog) -> None:
    """'Hundreds of POSTs, no writes' must be answerable from the log."""
    import httpx

    from app.libsql_http import TursoHTTPClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"type": "ok", "response": {"type": "execute", "result": {"cols": [], "rows": []}}}]},
        )

    TursoHTTPClient._COUNTS = {}
    TursoHTTPClient._TOTAL = 0
    client = TursoHTTPClient("https://db.turso.io", transport=httpx.MockTransport(handler))

    with caplog.at_level("DEBUG"):
        await client.execute("SELECT domain FROM sites WHERE domain = ?", ["a.in"])
        await client.execute("INSERT INTO sites (domain) VALUES (?)", ["a.in"])

    lines = [record.getMessage() for record in caplog.records if "pipeline execute" in record.getMessage()]
    assert len(lines) == 2
    assert "SELECT domain FROM sites" in lines[0]
    assert "INSERT INTO sites" in lines[1]
    await client.close()


async def test_the_seed_entry_and_exit_points_are_logged(temp_db, monkeypatch, caplog) -> None:
    store = get_store()
    seed = seed_crawler.CategorySeed("https://logged.gov.in", "Logged", "government")

    async def _fetch(client, url, settings, **kwargs):
        return ("<html><head><title>Logged portal</title>"
                "<meta name='description' content='Official portal with services and notices.'>"
                "</head><body><p>" + ("Official portal text for citizens. " * 4) + "</p></body></html>", 200)

    monkeypatch.setattr(seed_crawler, "fetch_page_status", _fetch)
    settings = EngineSettings(seed_crawl_enabled=True, respect_robots=False, crawl_delay_seconds=0.0)

    with caplog.at_level("DEBUG"):
        await seed_crawler.run_seed_pass(seeds=[seed], settings=settings, store=store)

    messages = [record.getMessage() for record in caplog.records]
    # INFO: the commit, and the pass summary.
    assert any("DB COMMIT SUCCESS: Indexed logged.gov.in" in m for m in messages)
    assert any("seed pass complete" in m for m in messages)
    # DEBUG: the per-URL trace stays available but off the INFO stream.
    assert any("fetched logged.gov.in -> 200" in m for m in messages)


def test_the_running_commit_is_reported(monkeypatch, caplog) -> None:
    from app import main

    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc1234")
    assert main._running_commit() == "abc1234 (RENDER_GIT_COMMIT)"

    monkeypatch.delenv("RENDER_GIT_COMMIT")
    for name in ("GIT_COMMIT", "SOURCE_VERSION", "COMMIT_SHA"):
        monkeypatch.delenv(name, raising=False)
    assert "unknown" in main._running_commit()


# --- the audit fetch: agent, timeout, redirects and TLS policy ------------

async def test_the_crawl_fetch_uses_the_browser_agent_and_15s_timeout() -> None:
    from app.crawler.crawler import fetch_page
    from app.engine.config import EngineSettings

    settings = EngineSettings()
    # Plain Chrome: a bot token in the agent earns a 403/406 from WAFs.
    assert "Chrome/128.0.0.0" in settings.crawl_user_agent
    assert "EyesearchBot" not in settings.crawl_user_agent
    assert settings.crawl_timeout_seconds == 15.0

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent")
        seen["Sec-Fetch-Mode"] = request.headers.get("sec-fetch-mode")
        return httpx.Response(200, text="<html><title>Portal</title></html>",
                              headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await fetch_page(client, "https://karnataka.gov.in/", settings)

    assert result is not None
    assert seen["ua"] == settings.crawl_user_agent
    assert "Sec-Fetch-Mode" in seen
    await client.aclose()


async def test_a_broken_certificate_is_retried_unverified(monkeypatch, caplog) -> None:
    """NIC portals present incomplete chains; the domain must not be lost."""
    from app.crawler import crawler as fetch_mod
    from app.engine.config import EngineSettings

    calls: list[str] = []

    async def _stream(client, url, headers, settings):
        calls.append("insecure" if getattr(client, "_insecure", False) else "verified")
        if len(calls) == 1:
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        return "<html><title>NIC portal</title></html>", 200

    monkeypatch.setattr(fetch_mod, "_stream_page", _stream)
    settings = EngineSettings(crawl_verify_tls="auto")

    with caplog.at_level("WARNING"):
        result = await fetch_mod.fetch_page(httpx.AsyncClient(), "https://nic.gov.in/", settings)

    assert result is not None, "the unverified retry should have succeeded"
    assert len(calls) == 2
    assert any("retrying this request unverified" in record.getMessage() for record in caplog.records)


async def test_the_tls_policy_can_forbid_the_fallback(monkeypatch) -> None:
    from app.crawler import crawler as fetch_mod
    from app.engine.config import EngineSettings

    async def _stream(client, url, headers, settings):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

    monkeypatch.setattr(fetch_mod, "_stream_page", _stream)
    settings = EngineSettings(crawl_verify_tls="always")

    assert await fetch_mod.fetch_page(httpx.AsyncClient(), "https://strict.gov.in/", settings) is None


# --- the production schema mismatch: crawl_backlog without `kind` ----------

async def test_a_legacy_backlog_table_without_kind_still_accepts_queries(temp_db) -> None:
    """The exact production error: no column named kind."""
    store = get_store()
    client = database.get_client()

    # Reproduce the old schema.
    await client.batch(
        [
            "DROP TABLE IF EXISTS crawl_backlog",
            "CREATE TABLE crawl_backlog (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "query TEXT NOT NULL, hits INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, processed_at TIMESTAMP)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_backlog_query ON crawl_backlog(query)",
        ]
    )
    store._backlog_kind = None  # force a fresh probe

    await store.enqueue_backlog("court", 1)

    assert await store.pending_backlog(10) == ["court"]


async def test_the_migration_adds_kind_and_is_verified(temp_db, caplog) -> None:
    store = get_store()
    client = database.get_client()
    await client.batch(
        [
            "DROP TABLE IF EXISTS crawl_backlog",
            "CREATE TABLE crawl_backlog (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "query TEXT NOT NULL, hits INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, processed_at TIMESTAMP)",
        ]
    )

    with caplog.at_level("INFO"):
        await database.initialize_schema()

    result = await client.execute("PRAGMA table_info(crawl_backlog)")
    columns = {str(row[1]).lower() for row in (getattr(result, "rows", ()) or ())}
    assert "kind" in columns
    assert any("migration applied: crawl_backlog.kind" in r.message for r in caplog.records)

    store._backlog_kind = None
    await store.enqueue_domain("maharashtra.gov.in")
    assert await store.pending_domains(5) == ["maharashtra.gov.in"]


async def test_a_failing_page_step_does_not_undo_the_site_commit(temp_db) -> None:
    """Sites commit in their own request, so a page failure cannot roll them back."""
    store = get_store()
    client = database.get_client()

    real_batch = client.batch
    calls: list[int] = []

    async def _batch(statements, **kwargs):
        statements = list(statements)
        calls.append(len(statements))
        if len(calls) == 2:
            # the page batch: pretend one step failed inside a 200 response
            client.last_errors = ["libsql step 0 failed: SQLITE_CONSTRAINT (statement: INSERT INTO pages …)"]
            return []
        return await real_batch(statements)

    client.batch = _batch  # type: ignore[method-assign]

    outcome = await store.replace_documents(
        [{"url": "https://committed.gov.in/", "canonical_url": "https://committed.gov.in/",
          "title": "Committed portal", "description": "Snippet.", "payload_bytes": 100}]
    )

    assert len(calls) == 2                     # sites, then pages — separate requests
    assert outcome["failed_statements"] == 1
    assert await store.count_documents() == 1  # the site row survived


# --- deep extraction: body text is indexed, not just meta tags ------------

def test_a_page_carries_up_to_1200_characters_of_body_text() -> None:
    from app.engine.text import extract_document_with_reason

    prose = "Karnataka publishes district-wise rainfall data every season. " * 60
    html = (
        "<html><head><title>Rainfall data</title>"
        "<meta name='description' content='District-wise rainfall for Karnataka.'>"
        f"</head><body><h1>Rainfall</h1><h2>Districts</h2><p>{prose}</p></body></html>"
    )
    document, reason = extract_document_with_reason("https://karnataka.gov.in/rain", html)

    assert document is not None, reason
    assert 1000 <= len(document.body_text) <= 1200, len(document.body_text)
    # the body opens with the page's own headings, then the prose
    assert "Karnataka publishes district-wise rainfall data" in document.body_text
    assert "Rainfall" in document.headings
    row = document.as_row()
    # the row may be trimmed to the budget, never expanded
    assert row["content"].startswith(document.body_text[:200])
    assert len(row["content"]) <= 1200
    assert row["headings"].startswith("Rainfall")


async def test_body_text_makes_a_body_only_match_findable(temp_db) -> None:
    """The point of deep extraction: a term that appears only in the body."""
    from app.engine.text import extract_document_with_reason

    html = (
        "<html><head><title>District portal</title>"
        "<meta name='description' content='Official district services.'>"
        "</head><body><h1>Services</h1>"
        f"<p>{'The sakala guarantee scheme covers 1,100 services. ' * 20}</p></body></html>"
    )
    document, reason = extract_document_with_reason("https://example.gov.in/", html)
    assert document is not None, reason

    store = get_store()
    await store.ingest_documents([document.as_row()])

    hits = await store.search_local("sakala", limit=3)
    assert hits, "the body term should match through page_fts"
    assert hits[0].url == "https://example.gov.in/"


async def test_the_row_budget_accommodates_the_deeper_body(temp_db) -> None:
    from app.engine.config import get_engine_settings

    cfg = get_engine_settings()
    assert cfg.max_payload_bytes >= 8192
    assert cfg.body_max_chars == 1200
    # the domain ceiling and the 300k page cap are unchanged
    assert cfg.max_site_payload_bytes == 10240
    assert cfg.storage_page_limit == 300000
