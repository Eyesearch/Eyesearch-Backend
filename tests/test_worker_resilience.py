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
