"""A background worker must survive its own failures.

A worker that ends stays dead until the next deploy: `/health` still answers and
search still serves the existing index, so the crawl stopping is invisible
unless someone reads the logs. These tests pin the three defences.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.database import normalize_legacy_subdomains
from app.engine.config import Settings
from app.indexer.scheduler import start_workers, stop_workers
from app.libsql_http import LibsqlError
from app.orchestrator import Orchestrator


def _settings(**overrides) -> Settings:
    base = dict(
        environment="test",
        crawler_enabled=True,
        revalidation_enabled=False,
        libsql_min_interval_seconds=0.0,
        crawl_batch_sleep_seconds=0.01,
        crawl_empty_backoff_seconds=0.01,
    )
    base.update(overrides)
    return Settings(**base)


async def test_the_crawl_loop_survives_a_refill_failure(store, client):
    """`refill()` raising used to escape run_forever and end the worker."""
    orchestrator = Orchestrator(
        client,
        _settings(),
        transport=httpx.MockTransport(lambda r: httpx.Response(404)),
    )
    calls = {"n": 0}

    async def failing_refill(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("Turso request failed: transient")

    orchestrator.frontier.refill = failing_refill

    task = asyncio.create_task(orchestrator.run_forever())
    try:
        await asyncio.sleep(0.2)
        assert not task.done(), "the loop must keep running after a refill error"
        assert calls["n"] >= 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await orchestrator.close()


async def test_the_supervisor_restarts_a_dead_worker(store, client):
    orchestrator = Orchestrator(
        client,
        _settings(),
        transport=httpx.MockTransport(lambda r: httpx.Response(404)),
    )
    runs = {"n": 0}

    async def dies_every_time():
        runs["n"] += 1
        raise RuntimeError("boom")

    handles = start_workers(orchestrator, _settings())
    task = handles.tasks["crawler"]
    task.cancel()
    await asyncio.sleep(0)
    try:
        # Replace the supervised worker with one that always dies.
        handles.tasks["crawler"] = asyncio.create_task(
            _supervise_import()("crawler", dies_every_time, handles)
        )
        await asyncio.sleep(0.05)
        assert runs["n"] >= 1
        assert handles.restarts.get("crawler", 0) >= 1
        assert handles.last_error.get("crawler", "").startswith("RuntimeError")
    finally:
        await stop_workers(handles)
        await orchestrator.close()


def _supervise_import():
    from app.indexer.scheduler import _supervise

    return _supervise


async def test_worker_status_reports_liveness(store, client):
    orchestrator = Orchestrator(
        client,
        _settings(crawler_enabled=False, revalidation_enabled=False),
        transport=httpx.MockTransport(lambda r: httpx.Response(404)),
    )
    handles = start_workers(orchestrator, _settings(crawler_enabled=False))
    try:
        assert handles.status() == {}
        assert handles.running() == []
    finally:
        await stop_workers(handles)
        await orchestrator.close()


async def test_a_database_error_does_not_stop_the_startup_migration(client):
    """A blip while normalising must not prevent the service booting."""

    class _Broken:
        async def execute(self, sql, args=()):
            raise LibsqlError("connection refused")

        async def batch(self, statements):
            raise LibsqlError("connection refused")

        async def close(self):
            pass

    assert await normalize_legacy_subdomains(_Broken()) == 0


# ---------------------------------------------------------------------------
# Boot resilience
# ---------------------------------------------------------------------------


def test_the_app_serves_health_when_the_database_bootstrap_fails(monkeypatch, tmp_path):
    """A broken database must degrade the app, not fail the deploy.

    The port only opens after the lifespan startup returns, so an exception
    here is a failed Render deploy with no import error to point at.
    """
    from fastapi.testclient import TestClient

    import app.main as main_module
    from app.engine.config import reset_settings_cache

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("ALLOW_LOCAL_DB", "true")
    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "degraded.db"))
    monkeypatch.setenv("CRAWLER_ENABLED", "false")
    monkeypatch.setenv("REVALIDATION_ENABLED", "false")
    reset_settings_cache()

    async def boom(client, settings):
        raise RuntimeError("Turso unreachable")

    monkeypatch.setattr(main_module, "_bootstrap_database", boom)

    try:
        with TestClient(main_module.app) as client:
            # Liveness stays 200 so the deploy succeeds; readiness reports down.
            live = client.get("/health")
            assert live.status_code == 200
            assert live.json()["db"] == "down"
            assert live.json()["status"] == "degraded"
            assert client.get("/ready").status_code == 503
            assert main_module.app.state.database_ready is False
    finally:
        reset_settings_cache()


def test_the_app_serves_health_when_the_bootstrap_times_out(monkeypatch, tmp_path):
    import asyncio as _asyncio

    from fastapi.testclient import TestClient

    import app.main as main_module
    from app.engine.config import reset_settings_cache

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("ALLOW_LOCAL_DB", "true")
    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "slow.db"))
    monkeypatch.setenv("CRAWLER_ENABLED", "false")
    monkeypatch.setenv("REVALIDATION_ENABLED", "false")
    monkeypatch.setenv("BOOT_DB_TIMEOUT_SECONDS", "0.05")
    reset_settings_cache()

    async def slow(client, settings):
        await _asyncio.sleep(5)

    monkeypatch.setattr(main_module, "_bootstrap_database", slow)

    try:
        with TestClient(main_module.app) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 503
            assert main_module.app.state.database_ready is False
    finally:
        reset_settings_cache()


def test_a_normal_boot_initialises_the_database(monkeypatch, tmp_path):
    """Guards the lifespan itself: a mis-decorated startup silently degrades.

    This is the test that was missing when `@asynccontextmanager` ended up on
    the bootstrap helper instead of `lifespan`: the app still answered /health
    with 200, but the schema was never created and every query failed.
    """
    from fastapi.testclient import TestClient

    import app.main as main_module
    from app.engine.config import reset_settings_cache

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("ALLOW_LOCAL_DB", "true")
    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "normal.db"))
    monkeypatch.setenv("CRAWLER_ENABLED", "false")
    monkeypatch.setenv("REVALIDATION_ENABLED", "false")
    reset_settings_cache()

    try:
        with TestClient(main_module.app) as client:
            body = client.get("/health").json()
            assert body["status"] == "ok", body
            assert body["db"] == "up", body
            assert client.get("/ready").status_code == 200
            assert main_module.app.state.database_ready is True
    finally:
        reset_settings_cache()
