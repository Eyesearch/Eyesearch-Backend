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
from urllib.parse import urlsplit, urlunsplit

import psycopg

import pytest
import pytest_asyncio

from app.database import initialize_database
from app.engine.config import Settings
from app.engine.store import IndexStore, PageDocument
from app.indexer.guard import StorageGuard
from app.postgres import PostgresClient


@pytest.fixture
def settings() -> Settings:
    """Settings for tests that are not about storage policy.

    The per-page body ceiling is off here on purpose. It is a real production
    policy — 40 characters — but a test writing a paragraph to check snippet or
    ranking behaviour would then be testing a 40-character prefix instead of the
    thing it names. The ceiling has its own tests, in test_domain_budget.py,
    which set it explicitly.
    """
    return Settings(
        environment="test",
        crawler_enabled=False,
        revalidation_enabled=False,
        crawl_batch_size=10,
        libsql_min_interval_seconds=0.0,
        page_body_char_ceiling=0,
    )


#: Where the test database lives.
#:
#: The default is the PostgreSQL service that `docker compose` starts, so a fresh
#: checkout runs `pytest` inside the container with no environment at all. An
#: earlier default pointed at a developer's local socket, which meant the suite
#: only ran on the machine it was written on — and failed everywhere else with a
#: connection error that looked like a code fault.
#:
#: Set TEST_DATABASE_URL to point somewhere else; a Unix socket path works as
#: long as it is passed explicitly.
TEST_DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://eyesearch:SecurePass_2026_Key@127.0.0.1:5432/eyesearch_test",
)


def _maintenance_dsn(dsn: str) -> str:
    """The same server, connected to the maintenance database.

    Needed to create `eyesearch_test` before anything can connect to it.
    """
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path="/postgres"))


def ensure_test_database(dsn: str) -> None:
    """Create the test database if it is not there yet.

    Autocommit is required: PostgreSQL refuses CREATE DATABASE inside a
    transaction block.
    """
    name = urlsplit(dsn).path.lstrip("/")
    if not name:
        return
    try:
        connection = psycopg.connect(_maintenance_dsn(dsn), autocommit=True, connect_timeout=5)
    except Exception:
        # No server to talk to. The test that needs one will report that far more
        # clearly than this would.
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if cursor.fetchone() is None:
                cursor.execute(f'CREATE DATABASE "{name}"')
    except Exception:
        pass
    finally:
        connection.close()


ensure_test_database(TEST_DSN)


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
async def store(client, settings) -> IndexStore:
    # The shared settings, so the store under test is not quietly using
    # production defaults the rest of the fixtures have turned off.
    return IndexStore(client, settings)


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
