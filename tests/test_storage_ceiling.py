"""The storage ceiling, expressed in megabytes and derived into a page count."""

from __future__ import annotations

from app.engine.config import Settings


def test_the_default_ceiling_is_the_4_5_gb_threshold():
    settings = Settings(_env_file=None, environment="test")

    assert settings.max_storage_mb == 4608.0
    assert settings.estimated_kb_per_page == 12.0
    assert settings.effective_storage_page_limit == 393_216


def test_the_derived_ceiling_matches_the_megabyte_budget():
    settings = Settings(_env_file=None, environment="test")

    implied_gb = (
        settings.effective_storage_page_limit
        * settings.estimated_kb_per_page
        / 1024
        / 1024
    )
    assert abs(implied_gb - 4.5) < 0.01


def test_the_ceiling_stays_under_a_5_gb_tier():
    settings = Settings(_env_file=None, environment="test")

    assert settings.max_storage_mb < 5 * 1024


def test_an_explicit_page_limit_overrides_the_derivation():
    settings = Settings(_env_file=None, environment="test", storage_page_limit=1_000)

    assert settings.effective_storage_page_limit == 1_000


def test_a_larger_page_estimate_lowers_the_page_ceiling():
    """Pages are heavier than expected, so fewer fit in the same MB budget."""
    settings = Settings(
        _env_file=None, environment="test", estimated_kb_per_page=24.0
    )

    assert settings.effective_storage_page_limit == 196_608


def test_the_guard_uses_the_derived_limit(client):
    """The orchestrator must hand the guard the derived value, not the raw 0."""
    from app.orchestrator import Orchestrator

    settings = Settings(_env_file=None, environment="test", crawler_enabled=False)
    orchestrator = Orchestrator(client, settings)
    try:
        assert orchestrator.guard.limit == 393_216
    finally:
        import asyncio

        asyncio.get_event_loop().run_until_complete(orchestrator.close())


# ---------------------------------------------------------------------------
# The doctor reports the same ceiling the guard enforces
# ---------------------------------------------------------------------------

from app.agents import load_agent  # noqa: E402
import pytest  # noqa: E402

doctor = load_agent("15_db_doctor_agent")

#: 4.5 GB, written out so the figure in the brief is the figure in the code.
FOUR_AND_A_HALF_GB = 4_831_838_208


def test_the_configured_ceiling_in_bytes():
    settings = Settings(_env_file=None, environment="test")

    assert settings.storage_ceiling_bytes == FOUR_AND_A_HALF_GB
    assert round(settings.storage_ceiling_bytes / 1024**3, 2) == 4.5


@pytest.mark.asyncio
async def test_the_doctor_reports_the_database_size(client):
    agent = doctor.DbDoctorAgent(client, Settings(_env_file=None, environment="test"))

    report = await agent.storage_check(FOUR_AND_A_HALF_GB)

    assert report["database_bytes"] > 0
    assert report["over_ceiling"] is False
    assert report["ceiling_bytes"] == FOUR_AND_A_HALF_GB


@pytest.mark.asyncio
async def test_the_doctor_flags_a_breach(client):
    """A ceiling of one byte is certainly exceeded — the flag has to work."""
    agent = doctor.DbDoctorAgent(client, Settings(_env_file=None, environment="test"))

    report = await agent.storage_check(1)

    assert report["over_ceiling"] is True
    assert report["ratio"] > 1


@pytest.mark.asyncio
async def test_a_doctor_run_reports_the_size_in_its_notes(client):
    agent = doctor.DbDoctorAgent(client, Settings(_env_file=None, environment="test"))

    report = await agent.run(dry_run=True)

    assert any("of 4608 MB" in note for note in report.notes), report.notes


@pytest.mark.asyncio
async def test_the_guard_pauses_on_the_byte_ceiling_not_only_the_page_count(client):
    """A database over 4.5 GB stops writes even with pages to spare."""
    from app.indexer.guard import StorageGuard

    guard = StorageGuard(client, limit=10_000_000, ceiling_bytes=1)

    state = await guard.storage_state()

    assert state.paused is True
    assert await guard.can_write() is False


@pytest.mark.asyncio
async def test_the_guard_allows_writes_under_both_ceilings(client):
    from app.indexer.guard import StorageGuard

    guard = StorageGuard(client, limit=10_000_000, ceiling_bytes=FOUR_AND_A_HALF_GB)

    state = await guard.storage_state()

    assert state.paused is False
    assert await guard.can_write() is True
