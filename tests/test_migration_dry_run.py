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
