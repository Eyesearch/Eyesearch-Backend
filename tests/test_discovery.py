"""Autonomous Indian domain discovery, the backlog queue and the storage ceiling."""

from __future__ import annotations

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
