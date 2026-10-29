"""Shared fixtures.

Every test runs against a real PostgreSQL database through the same
`LibsqlClient` interface production uses, so the SQL, the triggers, the
tsvector index and the atomic batches are the real ones.

The database comes from `TEST_DATABASE_URL`; each test gets a fresh schema, so
tests cannot see each other's rows. Point it at a throwaway database — the
fixture drops the `public` schema before every test.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from app.database import initialize_database
from app.engine.config import Settings
from app.engine.store import IndexStore, PageDocument
from app.indexer.guard import StorageGuard
from app.postgres import PostgresClient


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        crawl_batch_size=10,
        libsql_min_interval_seconds=0.0,
    )


#: Where the test database lives. Defaults to the local socket cluster the
#: migration was developed against, so `pytest` works without extra setup there.
TEST_DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres@/eyesearch_test?host=/scratch/work&port=55432",
)


@pytest_asyncio.fixture
async def client():
    """A PostgreSQL database with the production schema already applied."""
    database = PostgresClient(TEST_DSN, min_size=1, max_size=4)
    try:
        # A fresh schema per test: tests cannot see each other's rows, and a
        # leftover sequence from the previous test cannot shift an id.
        await database.execute("DROP SCHEMA IF EXISTS public CASCADE")
        await database.execute("CREATE SCHEMA public")
        await initialize_database(database)
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
