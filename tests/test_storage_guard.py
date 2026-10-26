"""Storage safety: the record ceiling and the quota breaker."""

from __future__ import annotations

import asyncio

import pytest

from app import database
from app.config import Settings
from app.engine.config import EngineSettings
from app.engine.store import get_store
from app.indexer import crawler as seed_crawler
from app.indexer import guard


@pytest.fixture()
async def temp_db(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/guard.db")
    await database.connect(settings)
    await database.initialize_schema()
    guard.reset_quota()
    yield
    guard.reset_quota()
    await database.close()


def _row(url: str, title: str = "Portal") -> dict:
    return {
        "url": url,
        "canonical_url": url,
        "title": title,
        "description": "Official portal with services.",
        "payload_bytes": 100,
    }


async def test_the_record_ceiling_pauses_the_pass(temp_db, monkeypatch, caplog) -> None:
    store = get_store()
    await store.ingest_documents([_row(f"https://site{i}.gov.in/") for i in range(3)])

    fetched: list[str] = []

    async def _fetch(*args, **kwargs):  # pragma: no cover - must not be called
        fetched.append("called")
        raise AssertionError("the ceiling is reached; nothing may be fetched")

    monkeypatch.setattr(seed_crawler, "fetch_page_status", _fetch)
    monkeypatch.setattr(
        seed_crawler, "get_engine_settings", lambda: EngineSettings(storage_page_limit=3)
    )
    settings = EngineSettings(seed_crawl_enabled=True, storage_page_limit=3)

    with caplog.at_level("WARNING"):
        report = await seed_crawler.run_seed_pass(
            seeds=[seed_crawler.CategorySeed("https://new.gov.in", "New", "government")],
            settings=settings,
            store=store,
        )

    assert report["paused"] is True
    assert report["stored"] == 0
    assert fetched == []
    assert any("Storage safety threshold reached" in r.getMessage() for r in caplog.records)


async def test_the_ceiling_also_blocks_the_discovery_pass(temp_db, monkeypatch) -> None:
    store = get_store()
    await store.ingest_documents([_row(f"https://site{i}.gov.in/") for i in range(2)])
    await store.enqueue_domain("queued.gov.in")

    async def _fetch(*args, **kwargs):  # pragma: no cover
        raise AssertionError("paused")

    monkeypatch.setattr(seed_crawler, "fetch_page_status", _fetch)
    settings = EngineSettings(seed_crawl_enabled=True, storage_page_limit=2)

    report = await seed_crawler.run_discovery_pass(store=store, settings=settings)

    assert report["paused"] is True
    assert report["attempted"] == 0


async def test_the_ceiling_blocks_the_autonomous_crawl(temp_db, monkeypatch) -> None:
    store = get_store()
    await store.ingest_documents([_row(f"https://site{i}.gov.in/") for i in range(2)])

    monkeypatch.setattr(
        seed_crawler, "get_engine_settings", lambda: EngineSettings(storage_page_limit=2)
    )
    report = await seed_crawler.run_autonomous_seed_crawl(store=store)

    assert report["skipped"] is True
    assert report["paused"] is True


async def test_a_quota_error_trips_the_breaker_and_stops_scheduling(temp_db, monkeypatch, caplog) -> None:
    store = get_store()
    client = database.get_client()
    real_batch = client.batch

    async def _quota(statements, **kwargs):
        raise database.DatabaseError("SQLITE_FULL: database or disk is full")

    monkeypatch.setattr(client, "batch", _quota)

    with caplog.at_level("ERROR"):
        with pytest.raises(Exception):
            await store.replace_documents([_row("https://full.gov.in/")])

    assert guard.quota_paused() is True
    assert any("STORAGE QUOTA EXHAUSTED" in r.getMessage() for r in caplog.records)

    # Every later pass returns immediately instead of tight-looping.
    calls: list[str] = []

    async def _fetch(*args, **kwargs):  # pragma: no cover
        calls.append("fetch")
        raise AssertionError("the breaker is open")

    monkeypatch.setattr(seed_crawler, "fetch_page_status", _fetch)
    monkeypatch.setattr(client, "batch", real_batch)

    report = await seed_crawler.run_seed_pass(
        seeds=[seed_crawler.CategorySeed("https://later.gov.in", "Later", "government")],
        settings=EngineSettings(seed_crawl_enabled=True),
        store=store,
    )

    assert report["paused"] is True
    assert calls == []


def test_quota_error_detection() -> None:
    assert guard.is_quota_error("SQLITE_FULL: database or disk is full")
    assert guard.is_quota_error("Storage quota exceeded for this database")
    assert guard.is_quota_error("no space left on device")
    assert not guard.is_quota_error("UNIQUE constraint failed: sites.domain")
    assert not guard.is_quota_error("no such column: kind")


async def test_the_breaker_resets_after_the_cooldown(temp_db, monkeypatch) -> None:
    guard.trip_quota("test")
    assert guard.quota_paused() is True

    monkeypatch.setattr(
        guard, "get_engine_settings", lambda: EngineSettings(storage_pause_seconds=0)
    )
    guard._breaker.tripped_at -= 120  # pretend the cooldown elapsed

    assert guard.quota_paused() is False


async def test_stats_report_the_storage_state(temp_db) -> None:
    import httpx

    from app.main import app

    store = get_store()
    await store.ingest_documents([_row("https://one.gov.in/")])

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=10) as client:
            body = (await client.get("/v1/stats")).json()

    assert body["storage"]["pages"] >= 1
    assert body["storage"]["limit"] > 0
    assert body["storage"]["paused"] is False
