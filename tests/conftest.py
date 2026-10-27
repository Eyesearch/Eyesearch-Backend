"""Shared fixtures.

Every test runs against an in-memory SQLite database through the same
`LibsqlClient` interface production uses, so the SQL, the triggers, the FTS
index and the atomic batches are the real ones. Nothing here touches the
network.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.database import initialize_database
from app.engine.config import Settings
from app.engine.store import IndexStore, PageDocument
from app.indexer.guard import StorageGuard
from app.libsql_http import LocalSqliteClient


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        crawl_batch_size=10,
        libsql_min_interval_seconds=0.0,
    )


@pytest_asyncio.fixture
async def client():
    """An in-memory database with the production schema already applied."""
    database = LocalSqliteClient(":memory:")
    await initialize_database(database)
    try:
        yield database
    finally:
        await database.close()


@pytest_asyncio.fixture
async def store(client) -> IndexStore:
    return IndexStore(client)


@pytest_asyncio.fixture
async def guard(client) -> StorageGuard:
    return StorageGuard(client, limit=300_000, ttl_seconds=60.0)


def make_document(
    url: str = "https://karnataka.gov.in/",
    *,
    domain: str = "karnataka.gov.in",
    title: str = "Karnataka State Portal",
    description: str = "Official portal of the Government of Karnataka.",
    body_text: str | None = None,
    digest: str = "",
) -> PageDocument:
    """A document that passes the quality gate unless told otherwise."""
    body = body_text
    if body is None:
        body = (
            "Karnataka is a state in the south western region of India. "
            "Its capital is Bengaluru, and the official portal publishes "
            "notifications, citizen services and departmental contact details."
        )
    return PageDocument(
        url=url,
        domain=domain,
        title=title,
        description=description,
        body_text=body,
        digest=digest,
    )
