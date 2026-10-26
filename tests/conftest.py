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


# A small map of URL → what the real page says, so the offline extractor can
# return realistic extracts without touching the network.
PAGE_TITLES = {
    "https://en.wikipedia.org/wiki/Espresso": (
        "Espresso — Wikipedia",
        "Espresso is a concentrated form of coffee brewed by forcing hot water.",
    ),
    "https://youtube.com/watch?v=dQw4w9WgXcQ": (
        "Espresso video guide",
        "A short walkthrough of home espresso machines.",
    ),
    "https://example.org/only": ("Only result", "The chain lost two providers."),
    "https://example.org/espresso-partial": ("Espresso partial", "Espresso with one provider."),
}


@pytest.fixture
def extract_stub(monkeypatch):
    """Replace the network extractor with a deterministic offline one."""
    from app.engine.text import ExtractedDocument
    from app.engine.urls import normalise_url
    from app.services import search_service

    async def _extract(urls, *, client=None, settings=None, limit=None):
        documents = []
        for raw_url in urls:
            # The real extractor normalises the URL before storing it; so does this.
            url = normalise_url(raw_url)
            title, snippet = PAGE_TITLES.get(
                url,
                (f"{url.rstrip('/').split('/')[-1] or url} — {url.split('/')[2]}", "Extracted page."),
            )
            host = url.split("/")[2].removeprefix("www.")
            documents.append(
                ExtractedDocument(
                    url=url,
                    canonical_url=url,
                    domain=host,
                    root_domain=host,
                    domain_class="static",
                    title=title[:100],
                    snippet=snippet[:200],
                    content=f"{title}. {snippet}"[:1200],
                    language="en",
                    favicon_url=None,
                )
            )
        return documents[: (limit or len(documents))]

    monkeypatch.setattr(search_service, "extract_candidates", _extract)
    return _extract
