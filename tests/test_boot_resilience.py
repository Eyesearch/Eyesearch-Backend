"""Boot must not lose indexes to one slow statement, and must not stall on it."""

from __future__ import annotations

import pytest

from app.database import apply_schema_statements, backfill_category_ids
from app.libsql_http import LibsqlError


class Recorder:
    """A client that records statements and can fail a chosen one."""

    #: The backfill is a PostgreSQL-only repair; the recorder must say so.
    dialect = "postgres"

    def __init__(self, fail_on: str | None = None):
        self.statements: list[str] = []
        self._fail_on = fail_on

    async def execute(self, sql, args=()):
        self.statements.append(sql)
        if self._fail_on and self._fail_on in sql:
            raise LibsqlError("statement timed out")
        return type("R", (), {"rows_affected": 0, "scalar": lambda self, i=0: 0})()


@pytest.mark.asyncio
async def test_one_failing_statement_does_not_discard_the_rest():
    """The batch was one transaction: a failure rolled back everything after it,
    which is how the GIN index went missing while the log only showed a timeout."""
    client = Recorder(fail_on="UPDATE pages")

    failures = await apply_schema_statements(
        client,
        [
            "CREATE INDEX IF NOT EXISTS a ON pages(x)",
            "UPDATE pages SET y = 1",
            "CREATE INDEX IF NOT EXISTS b ON pages(y)",
        ],
    )

    assert len(failures) == 1
    assert any("CREATE INDEX IF NOT EXISTS b" in sql for sql in client.statements)


@pytest.mark.asyncio
async def test_a_clean_run_reports_no_failures():
    client = Recorder()

    failures = await apply_schema_statements(client, ["CREATE INDEX IF NOT EXISTS a ON pages(x)"])

    assert failures == []


@pytest.mark.asyncio
async def test_the_backfill_is_set_based_not_row_by_row():
    """It used to be `UPDATE pages SET category = category`, which fired the
    trigger once per row and ran past the statement timeout at boot."""
    client = Recorder()

    await backfill_category_ids(client)

    assert len(client.statements) == 2
    for sql in client.statements:
        assert "FROM categories c" in sql
        assert "SET category =" not in sql


@pytest.mark.asyncio
async def test_the_backfill_does_not_fire_the_row_trigger():
    """The trigger is UPDATE OF category; the backfill writes category_id."""
    client = Recorder()

    await backfill_category_ids(client)

    for sql in client.statements:
        assert "category_id" in sql


@pytest.mark.asyncio
async def test_a_failing_backfill_does_not_stop_the_boot():
    client = Recorder(fail_on="UPDATE pages")

    assert await backfill_category_ids(client) == 0


def test_the_category_name_is_indexed_before_any_backfill():
    """The trigger looks a category up by name; without the index each row scans."""
    from app.schema_postgres import POSTGRES_SCHEMA_STATEMENTS

    name_index = next(
        i for i, sql in enumerate(POSTGRES_SCHEMA_STATEMENTS) if "idx_categories_name" in sql
    )
    assert name_index < len(POSTGRES_SCHEMA_STATEMENTS)


def test_the_schema_no_longer_carries_a_mass_update():
    from app.schema_postgres import POSTGRES_SCHEMA_STATEMENTS

    for sql in POSTGRES_SCHEMA_STATEMENTS:
        assert "UPDATE pages SET category = category" not in sql


@pytest.mark.asyncio
async def test_boot_creates_every_page_index(client):
    """A boot that silently loses indexes is worse than one that fails."""
    from app.database import initialize_database

    await initialize_database(client)

    rows = await client.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'pages'"
    )
    names = {row[0] for row in rows.rows}
    assert "idx_pages_search" in names, names
    assert "idx_pages_category_id" in names, names


@pytest.mark.asyncio
async def test_boot_links_existing_rows_to_a_category(client):
    from app.database import initialize_database

    await initialize_database(client)
    await client.execute(
        "INSERT INTO sites (domain, canonical_url) VALUES ('a.gov.in', 'https://a.gov.in/')"
    )
    await client.execute(
        "INSERT INTO pages (site_id, url, title, description, body_text, category) "
        "SELECT site_id, 'https://a.gov.in/x', 'T', 'd', 'body', 'government' FROM sites LIMIT 1"
    )
    await client.execute("UPDATE pages SET category_id = NULL")

    linked = await backfill_category_ids(client)

    assert linked >= 1
    left = await client.execute("SELECT COUNT(*) FROM pages WHERE category_id IS NULL")
    assert int(left.scalar(0) or 0) == 0


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_analyses_the_tables_the_planner_uses(client):
    """Without statistics the planner guesses the row count, and a guess of a
    hundred rows makes a sequential scan look cheaper than the GIN index."""
    from app.database import initialize_database, refresh_statistics

    await initialize_database(client)

    assert await refresh_statistics(client) == []


@pytest.mark.asyncio
async def test_statistics_cover_pages(client):
    from app.database import STATISTICS_TABLES

    assert "pages" in STATISTICS_TABLES


@pytest.mark.asyncio
async def test_a_missing_table_does_not_stop_the_others():
    """A first boot analyses tables that may not exist yet; that is not an error."""
    client = Recorder(fail_on="ANALYZE pages")

    await apply_schema_statements(client, ["ANALYZE pages", "ANALYZE sites"])

    assert any("ANALYZE sites" in sql for sql in client.statements)


@pytest.mark.asyncio
async def test_the_ranked_query_uses_the_full_text_index_when_rows_are_many(client, store):
    """With real statistics the planner may still choose a scan — that is correct
    when a query matches every row. What must never happen is the index being
    absent, which is what made this a sequential scan at all."""
    from app.database import initialize_database

    await initialize_database(client)

    rows = await client.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'pages'")
    assert "idx_pages_search" in {row[0] for row in rows.rows}


@pytest.mark.asyncio
async def test_a_timed_out_bootstrap_is_retried_in_the_background(monkeypatch):
    """A slow database at boot must not leave the process degraded for ever.

    The bootstrap runs before the port binds, so it is bounded; a timeout used
    to set `database_ready` False and never look again, and `/health` reported
    `db: down` while every query worked.
    """
    import asyncio
    import types

    from app import main as main_module
    from app.engine.config import Settings

    attempts = []

    async def fake_bootstrap(client, settings):
        attempts.append(1)
        if len(attempts) == 1:
            raise asyncio.TimeoutError

    monkeypatch.setattr(main_module, "_bootstrap_database", fake_bootstrap)
    app = types.SimpleNamespace(state=types.SimpleNamespace(database_ready=False))
    settings = Settings(
        _env_file=None,
        boot_db_timeout_seconds=1.0,
        boot_db_retry_seconds=0.01,
    )

    task = asyncio.create_task(main_module._retry_bootstrap(app, None, settings))
    for _ in range(800):
        if app.state.database_ready:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert app.state.database_ready is True
    assert len(attempts) >= 2


@pytest.mark.asyncio
async def test_a_slow_count_does_not_report_the_database_as_down(monkeypatch):
    """One aggregate that times out is not a dead database.

    `/health` counts four things. When the `indexed` count was slow it raised,
    the handler caught it, and the probe answered `db: down` with zeros - so
    monitoring saw a dead service while the crawler wrote pages and search
    answered. The probe now asks `SELECT 1` before saying that.
    """
    import types

    from app import main as main_module
    from app.engine.config import Settings

    class Client:
        async def execute(self, sql, params=()):
            return types.SimpleNamespace(scalar=lambda _i=0: 1, rows=[(1,)])

    class BrokenStore:
        def __init__(self, client, settings):
            pass

        async def stats(self):
            raise RuntimeError("postgres statement timed out")

    monkeypatch.setattr(main_module, "IndexStore", BrokenStore)

    request = types.SimpleNamespace(
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(
                settings=Settings(_env_file=None),
                database_ready=True,
                client=Client(),
                orchestrator=None,
                workers=None,
            )
        )
    )

    payload = await main_module.health_payload(request)

    assert payload["db"] == "up"
    assert payload["status"] == "ok"
    assert payload["warnings"], "the failed count must be reported, not hidden"
    assert "timed out" in payload["warnings"][0]
