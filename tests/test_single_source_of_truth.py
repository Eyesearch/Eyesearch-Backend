"""Maintenance code follows the live settings, not constants of its own."""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.engine.config import Settings
from app.engine.store import PageDocument

RuleEnforcerAgent = load_agent("11_rule_enforcer_agent").RuleEnforcerAgent
DefectAuditor = load_agent("14_king_orchestrator_agent").DefectAuditor
classify_row = load_agent("14_king_orchestrator_agent").classify_row

BODY = "karnataka health district notification office government scheme passport " * 4


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, environment="test", **overrides)


def test_the_word_floor_comes_from_settings():
    """A row that is thin at fifteen words is not thin at five."""
    strict = classify_row(
        page_id=1, url="https://a.in/x", brand_name="A",
        body_text="one two three four five six seven", language="en",
        ceiling=200, thin_words=15,
    )
    loose = classify_row(
        page_id=1, url="https://a.in/x", brand_name="A",
        body_text="one two three four five six seven", language="en",
        ceiling=200, thin_words=5,
    )

    assert [d.kind for d in strict] == ["thin_content"]
    assert [d.kind for d in loose] == []


def test_the_auditor_reads_the_configured_floor():
    auditor = DefectAuditor(client=None, settings=settings(min_page_words=3))
    assert auditor.thin_words == 3

    auditor = DefectAuditor(client=None, settings=settings(min_page_words=40))
    assert auditor.thin_words == 40


@pytest.mark.asyncio
async def test_the_enforcer_uses_the_configured_page_cap(client):
    """Change the cap and the purge follows it, with no code change."""
    from app.engine.store import IndexStore

    index = IndexStore(client, settings(page_body_char_ceiling=0))
    await index.persist_batch(
        [
            PageDocument(
                url=f"https://a.gov.in/{i}", domain="a.gov.in", title=f"Page {i}",
                description="d", body_text=BODY, digest=f"sst-{i}", brand_name="A",
            )
            for i in range(5)
        ]
    )

    strict = RuleEnforcerAgent(client, settings(max_pages_per_domain=2))
    domains, purged = await strict.enforce_page_cap(dry_run=True)

    assert domains == 1
    assert purged == 3


@pytest.mark.asyncio
async def test_the_enforcer_uses_the_configured_ceiling(client):
    from app.engine.store import IndexStore

    index = IndexStore(client, settings(page_body_char_ceiling=0))
    await index.persist_batch(
        [
            PageDocument(
                url="https://b.gov.in/1", domain="b.gov.in", title="Long",
                description="d", body_text=BODY, digest="sst-ceil", brand_name="B",
            )
        ]
    )

    agent = RuleEnforcerAgent(client, settings(page_body_char_ceiling=50))
    trimmed = await agent.enforce_body_ceiling(dry_run=True)

    assert trimmed == 1


def test_the_new_settings_have_the_agreed_defaults():
    live = Settings(_env_file=None)

    assert live.min_page_words == 15
    assert live.legacy_truncated_chars == 45
    assert live.revalidation_min_sleep_seconds == 30.0
    assert live.max_pages_per_domain == 25
    assert live.page_body_char_ceiling == 200
