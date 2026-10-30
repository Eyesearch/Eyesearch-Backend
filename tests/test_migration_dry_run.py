"""A dry run must not write. This is the test that was missing."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_categories_16.py"


def load_script():
    spec = importlib.util.spec_from_file_location("migrate_categories_16", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_categories_16"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_a_dry_run_changes_nothing(client):
    """Every execute() on this client commits its own transaction, so a dry run
    cannot write-then-roll-back — it has to count instead."""
    module = load_script()

    before = await client.execute("SELECT COUNT(*) FROM categories")
    await module.migrate(client, dry_run=True)
    after = await client.execute("SELECT COUNT(*) FROM categories")

    assert before.scalar(0) == after.scalar(0)


@pytest.mark.asyncio
async def test_a_dry_run_reports_what_is_missing(client):
    module = load_script()

    report = await module.migrate(client, dry_run=True)

    assert "categories_missing" in report
    assert report["categories_missing"] == 0  # a fresh test database is seeded


@pytest.mark.asyncio
async def test_the_real_run_leaves_no_orphaned_pages(client):
    module = load_script()

    report = await module.migrate(client, dry_run=False)

    assert report["orphaned_pages"] == 0


@pytest.mark.asyncio
async def test_the_migration_is_idempotent(client):
    """Running it twice must be as safe as running it once."""
    module = load_script()

    await module.migrate(client, dry_run=False)
    second = await module.migrate(client, dry_run=False)

    assert second["orphaned_pages"] == 0
    assert all(count == 0 for key, count in second.items() if key.startswith("renamed:"))


@pytest.mark.asyncio
async def test_it_survives_a_database_that_already_has_the_new_codes(client):
    """The production failure: initialize_database seeds FIN, then the rename of
    FINANCE tries to take a code that is already taken."""
    module = load_script()

    for code, name in (
        ("FINANCE", "business_finance"),
        ("PAY", "payments"),
        ("COMMERCE", "commerce"),
        ("AGRI", "agriculture_rural"),
    ):
        await client.execute(
            "INSERT INTO categories (code, name) VALUES (?, ?) ON CONFLICT (code) DO NOTHING",
            (code, name),
        )

    report = await module.migrate(client, dry_run=False)

    assert report["orphaned_pages"] == 0
    remaining = await client.execute("SELECT code FROM categories ORDER BY code")
    codes = [str(row[0]) for row in remaining.rows]
    assert len(codes) == 16
    for old in ("FINANCE", "PAY", "COMMERCE", "AGRI"):
        assert old not in codes, old


@pytest.mark.asyncio
async def test_a_page_under_a_retired_code_keeps_its_category(client):
    """Re-pointing, not deleting: the page must not lose its classification."""
    module = load_script()
    await client.execute(
        "INSERT INTO categories (code, name) VALUES ('FINANCE', 'business_finance') "
        "ON CONFLICT (code) DO NOTHING"
    )
    await client.execute(
        "INSERT INTO pages (url, title, description, body_text, digest) "
        "VALUES ('https://a.gov.in/x', 'T', 'd', 'body', 'mig-1')"
    )
    await client.execute(
        "UPDATE pages SET category_id = (SELECT id FROM categories WHERE code = 'FINANCE')"
    )

    await module.migrate(client, dry_run=False)

    result = await client.execute(
        "SELECT c.code FROM pages p JOIN categories c ON c.id = p.category_id "
        "WHERE p.url = 'https://a.gov.in/x'"
    )
    assert str(result.rows[0][0]) == "FIN"
