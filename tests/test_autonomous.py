"""Zero-manual ingestion: coverage detection and the lifecycle's own crawl."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import database
from app.config import Settings
from app.engine.config import EngineSettings
from app.engine.store import get_store
from app.indexer import crawler as seed_crawler


@pytest.fixture()
async def temp_db(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/autonomous.db")
    await database.connect(settings)
    await database.initialize_schema()
    yield
    await database.close()


def _html(title: str) -> str:
    return (
        f"<html><head><title>{title}</title>"
        f"<meta name='description' content='{title} official page.'></head>"
        "<body><nav><a href='/about'>About</a><a href='/contact'>Contact</a></nav>"
        f"<article><p>{title} is an authoritative Indian source. It publishes official "
        "information. The portal covers services and notices. Readers use it daily.</p></article></body></html>"
    )


async def test_coverage_is_low_on_an_empty_index(temp_db) -> None:
    state = await seed_crawler.coverage()
    assert state["sites"] == 0
    assert state["sufficient"] is False
    assert state["seed_domains"] >= 20
    assert state["threshold"] >= 1


async def test_autonomous_crawl_skips_when_the_index_is_covered(temp_db, monkeypatch) -> None:
    store = get_store()
    for index in range(6):
        await store.ingest_documents(
            [
                {
                    "url": f"https://site{index}.example.in/",
                    "canonical_url": f"https://site{index}.example.in/",
                    "title": f"Covered site {index}",
                    "description": "Already indexed.",
                    "payload_bytes": 120,
                }
            ]
        )

    async def _never(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("the index is covered; the crawler must stay idle")

    monkeypatch.setattr(seed_crawler, "run_seed_pass", _never)
    report = await seed_crawler.run_autonomous_seed_crawl()

    assert report["skipped"] is True
    assert report["sufficient"] is True


async def test_autonomous_crawl_populates_an_empty_index(temp_db, monkeypatch) -> None:
    async def _fetch(client, url, settings, **kwargs):
        return _html("Seed page"), 200

    monkeypatch.setattr(seed_crawler, "fetch_page", _fetch)
    monkeypatch.setattr(
        seed_crawler,
        "get_engine_settings",
        lambda: EngineSettings(
            seed_crawl_enabled=True,
            respect_robots=False,
            crawl_delay_seconds=0.0,
            seed_crawl_pages_per_domain=1,
            seed_crawl_concurrency=5,
            seed_coverage_min_sites=3,
        ),
    )

    report = await seed_crawler.run_autonomous_seed_crawl()

    assert report["skipped"] is False
    assert report["stored"] >= 3
    state = await seed_crawler.coverage()
    assert state["sites"] >= 3
    assert state["sufficient"] is True
    assert state["covered"] >= 3

    # The crawl wrote real pages, and they are searchable immediately.
    assert await get_store().count_pages() >= 3
    hits = await get_store().search_local("seed page", limit=5)
    assert hits


async def test_autonomous_crawl_survives_a_failing_seed(temp_db, monkeypatch) -> None:
    calls = {"n": 0}

    async def _flaky(client, url, settings, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectTimeout("seed unreachable")
        return _html("Surviving seed"), 200

    monkeypatch.setattr(seed_crawler, "fetch_page", _flaky)
    monkeypatch.setattr(
        seed_crawler,
        "get_engine_settings",
        lambda: EngineSettings(
            seed_crawl_enabled=True,
            respect_robots=False,
            crawl_delay_seconds=0.0,
            seed_crawl_pages_per_domain=1,
            seed_coverage_min_sites=99,
        ),
    )

    report = await seed_crawler.run_autonomous_seed_crawl()

    # Two domains blew up, the pass still completed and stored the rest.
    assert report["skipped"] is False
    assert report["stored"] >= 1


async def test_startup_does_not_wait_for_the_crawler(temp_db, monkeypatch) -> None:
    """The port binds and /health answers while the crawl is still running."""
    from app.indexer import scheduler as seed_scheduler
    from app.main import app

    started = asyncio.Event()

    async def _slow_crawl(*args, **kwargs):
        started.set()
        await asyncio.sleep(3)  # a crawl that outlives the request below
        return {"skipped": False}

    # The scheduler binds the function at import time, so patch its reference.
    monkeypatch.setattr(seed_scheduler, "run_autonomous_seed_crawl", _slow_crawl)
    # The scheduler is the piece that owns the task; enable it for this test.
    monkeypatch.setattr(
        seed_scheduler,
        "get_engine_settings",
        lambda: EngineSettings(seed_crawl_enabled=True, seed_crawl_interval_seconds=86400),
    )
    monkeypatch.setattr(
        seed_crawler,
        "coverage",
        lambda *a, **k: asyncio.sleep(0, result={"sites": 0, "sufficient": False, "covered": 0, "seed_domains": 24, "threshold": 5}),
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=2.0) as client:
            response = await client.get("/health")
            assert response.status_code in {200, 503}
            await asyncio.wait_for(started.wait(), timeout=1.0)
            # The crawl is still in flight; the server answered anyway.
            assert not _slow_crawl.__name__ == ""
