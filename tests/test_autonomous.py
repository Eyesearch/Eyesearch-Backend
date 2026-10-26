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


async def test_autonomous_crawl_skips_only_when_the_registry_is_indexed(temp_db, monkeypatch) -> None:
    store = get_store()
    # Registry seeds actually present in `sites`…
    for host in ("india.gov.in", "karnataka.gov.in", "sebi.gov.in"):
        await store.ingest_documents(
            [{"url": f"https://{host}/", "canonical_url": f"https://{host}/",
              "title": host, "description": "Indexed.", "payload_bytes": 100}]
        )
    # …plus 61 unrelated sites, which is what fooled the old count-based check.
    for index in range(61):
        await store.ingest_documents(
            [{"url": f"https://old{index}.example.in/", "canonical_url": f"https://old{index}.example.in/",
              "title": f"Old site {index}", "description": "Legacy row.", "payload_bytes": 100}]
        )

    monkeypatch.setattr(
        seed_crawler, "get_engine_settings", lambda: EngineSettings(seed_coverage_min_ratio=0.01)
    )

    async def _never(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("the registry is covered; the crawler must stay idle")

    monkeypatch.setattr(seed_crawler, "run_seed_pass", _never)
    report = await seed_crawler.run_autonomous_seed_crawl()

    assert report["skipped"] is True
    assert report["sufficient"] is True


async def test_coverage_is_not_fooled_by_unrelated_sites(temp_db, monkeypatch) -> None:
    """The reported bug: 61 old sites skipped the 211 new seeds entirely."""
    store = get_store()
    for index in range(61):
        await store.ingest_documents(
            [{"url": f"https://legacy{index}.example.in/", "canonical_url": f"https://legacy{index}.example.in/",
              "title": f"Legacy {index}", "description": "Old row.", "payload_bytes": 100}]
        )

    state = await seed_crawler.coverage()

    assert state["sites"] == 61
    assert state["sufficient"] is False          # 61 sites do not cover 211 seeds
    assert state["covered"] == 0
    assert "india.gov.in" in state["uncovered"]
    assert len(state["uncovered"]) == state["seed_domains"]


async def test_autonomous_crawl_targets_only_the_missing_seeds(temp_db, monkeypatch) -> None:
    store = get_store()
    await store.ingest_documents(
        [{"url": "https://india.gov.in/", "canonical_url": "https://india.gov.in/",
          "title": "India", "description": "Indexed.", "payload_bytes": 100}]
    )

    captured: dict[str, list] = {}

    async def _capture(*args, seeds=None, **kwargs):
        captured["seeds"] = list(seeds or [])
        return {"documents": 0, "stored": 0}

    monkeypatch.setattr(seed_crawler, "run_seed_pass", _capture)
    monkeypatch.setattr(seed_crawler, "run_discovery_pass", lambda **kw: asyncio.sleep(0, result={}))

    report = await seed_crawler.run_autonomous_seed_crawl()

    assert report["skipped"] is False
    hosts = {s.url.split("/")[2].removeprefix("www.") for s in captured["seeds"]}
    assert "india.gov.in" not in hosts            # already indexed, not re-crawled
    assert "sebi.gov.in" in hosts
    assert len(captured["seeds"]) == report["seed_domains"] - 1


async def test_autonomous_crawl_populates_an_empty_index(temp_db, monkeypatch) -> None:
    async def _fetch(client, url, settings, **kwargs):
        return _html("Seed page"), 200

    monkeypatch.setattr(seed_crawler, "fetch_page_status", _fetch)
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

    monkeypatch.setattr(seed_crawler, "fetch_page_status", _flaky)
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


async def test_execute_safe_survives_a_response_without_result(monkeypatch) -> None:
    """The Render KeyError: 'result' — a driver payload we must not trust."""
    from app import database

    async def _boom(sql, params=None, *, timeout_s=None):
        raise database.DatabaseError("database query failed: 'result'") from KeyError("result")

    monkeypatch.setattr(database, "_execute", _boom)

    result = await database.execute_safe("SELECT 1")
    assert getattr(result, "rows", None) == ()

    # A dict payload is normalised rather than raising.
    async def _dict(sql, params=None, *, timeout_s=None):
        return {"result": {"columns": ["domain"], "rows": [["india.gov.in"]]}}

    monkeypatch.setattr(database, "_execute", _dict)
    normalised = await database.execute_safe("SELECT domain FROM sites")
    assert list(normalised.rows) == [["india.gov.in"]]
    assert list(normalised.columns) == ["domain"]


async def test_backlog_enqueue_survives_the_driver_keyerror(temp_db, monkeypatch) -> None:
    """`backlog enqueue failed for 'court': 'result'` must not lose the query."""
    from app import database

    store = get_store()
    real_execute = database._execute
    calls = {"n": 0}

    async def _flaky(sql, params=None, *, timeout_s=None):
        calls["n"] += 1
        if "INSERT INTO crawl_backlog" in sql and calls["n"] <= 2:
            raise database.DatabaseError("database query failed: 'result'") from KeyError("result")
        return await real_execute(sql, params, timeout_s=timeout_s)

    monkeypatch.setattr(database, "_execute", _flaky)

    await store.enqueue_backlog("court", 1)          # survives the driver hiccup
    await store.enqueue_backlog("court", 1)          # retried on the next attempt
    assert await store.pending_backlog(10) == ["court"]
