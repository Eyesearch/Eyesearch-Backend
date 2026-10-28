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
