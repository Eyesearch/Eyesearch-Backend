"""Tests for the database target: which database the process writes to, and proof.

These exist because of a real failure mode: the service served live results
while the cloud database saw no reads and no writes, because it had quietly
fallen back to a local file.
"""

from __future__ import annotations

import logging

import pytest

from app import database
from app.config import Settings
from app.engine.autoingest import auto_ingest
from app.errors import DatabaseError
from app.services.live_retrieval import RawResult


@pytest.fixture(autouse=True)
async def clean_client():
    yield
    await database.close()


def test_describe_target_distinguishes_remote_from_local() -> None:
    remote = database.describe_target(
        Settings(turso_db_url="libsql://eyesearch-user.turso.io", turso_auth_token="secret")
    )
    assert remote["kind"] == "remote"
    assert remote["has_token"] == "yes"
    assert "secret" not in remote["url"]

    local = database.describe_target(Settings(turso_db_url="file:./local.db"))
    assert local["kind"] == "local"
    assert local["has_token"] == "no"
    assert local["url"] == "file:./local.db"


def test_safe_url_strips_credentials_and_tokens() -> None:
    assert database.safe_url("libsql://user:pass@db.turso.io") == "libsql://db.turso.io"
    assert database.safe_url("https://db.turso.io/x").endswith("authToken=***&other=1")


async def test_remote_url_without_token_is_refused() -> None:
    settings = Settings(turso_db_url="libsql://eyesearch-user.turso.io", turso_auth_token="")
    with pytest.raises(DatabaseError) as error:
        await database.connect(settings)
    assert "TURSO_AUTH_TOKEN" in str(error.value)


def test_production_requires_a_remote_database() -> None:
    production_local = Settings(environment="production", turso_db_url="file:./local.db")
    assert production_local.is_remote_db is False
    assert production_local.requires_remote_db is True

    allowed = Settings(environment="production", turso_db_url="file:./local.db", allow_local_db=True)
    assert allowed.requires_remote_db is False

    production_remote = Settings(environment="production", turso_db_url="libsql://db.turso.io")
    assert production_remote.requires_remote_db is True
    assert production_remote.is_remote_db is True


async def test_lifespan_refuses_to_boot_in_production_on_a_local_file(monkeypatch) -> None:
    from app import main as main_module

    production = Settings(environment="production", turso_db_url="file:./local.db")
    monkeypatch.setattr(main_module, "get_settings", lambda: production)

    with pytest.raises(RuntimeError) as error:
        async with main_module.app.router.lifespan_context(main_module.app):
            pass
    assert "refusing to start" in str(error.value)


async def test_auto_ingest_logs_the_query_and_target(temp_db, caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.engine.autoingest")

    results = [
        RawResult(
            title="Github · GitHub",
            url="https://github.com",
            snippet="GitHub is where people build software.",
            source="duckduckgo",
        )
    ]
    outcome = await auto_ingest(results, query="github")

    assert outcome["inserted"] == 1
    assert "Auto-ingested 1 documents for query: github into local SQLite" in caplog.text


async def test_health_reports_which_database_is_in_use(client) -> None:
    response = await client.get("/health")
    body = response.json()
    assert body["db_target"].startswith("local:file:")
    assert body["db"] == "up"
