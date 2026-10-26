"""Test harness: a throwaway SQLite file and the real lifespan, no network."""

from __future__ import annotations

import os
import tempfile
from typing import AsyncIterator

import httpx
import pytest

_TMP_DIR = tempfile.mkdtemp(prefix="eyesearch-test-")
os.environ.setdefault("TURSO_DB_URL", f"file:{_TMP_DIR}/test.db")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("CHAIN_BUDGET_S", "3.0")
# The smoke suite covers the emergency live path (LIVE_SEARCH_ENABLED=true). The
# default — no external fetch on the request path — is covered by test_engine.py
# and test_budget.py.
os.environ.setdefault("LIVE_SEARCH_ENABLED", "true")
# Background workers and discovery stay off in tests: the suite calls the worker
# functions directly, and a scheduled pass would reach the network.
os.environ.setdefault("REVALIDATE_ENABLED", "false")
os.environ.setdefault("BACKLOG_WORKER_ENABLED", "false")
os.environ.setdefault("BACKLOG_DISCOVERY_ENABLED", "false")

from app.main import app  # noqa: E402  (env must be set before settings load)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


@pytest.fixture()
async def temp_db(tmp_path):
    """A fresh database with the gateway and engine schema applied."""
    from app import database
    from app.config import Settings
    from app.engine.store import get_store

    settings = Settings(turso_db_url=f"file:{tmp_path}/engine.db")
    await database.connect(settings)
    await database.initialize_schema()
    await get_store().ensure_schema()
    yield
    await database.close()
