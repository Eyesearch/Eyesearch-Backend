"""A ceiling is a stop, not a quota to fill."""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.engine.store import PageDocument

frontier = load_agent("01_frontier_agent")
enforcer = load_agent("11_rule_enforcer_agent")
RuleEnforcerAgent = enforcer.RuleEnforcerAgent

BODY = "distinct prose about the department and its services for citizens. " * 2


def page(domain: str, index: int, quality: float = 1.0) -> PageDocument:
    return PageDocument(
        url=f"https://{domain}/p{index}", domain=domain, title=f"Page {index}",
        description="d", body_text=BODY, digest=f"{domain}-{index}",
        brand_name="D", quality_score=quality,
    )


@pytest.mark.asyncio
async def test_a_site_with_three_pages_is_not_pushed_to_its_ceiling(client, store):
    """Three good pages on a government host is a finished site, not a third of
    a quota to fill."""
    await store.persist_batch([page("small.gov.in", i) for i in range(3)])

    agent = RuleEnforcerAgent(client, load_agent("01_frontier_agent").get_settings())
    domains, purged = await agent.enforce_page_cap(dry_run=True)

    assert domains == 0
    assert purged == 0


@pytest.mark.asyncio
async def test_a_host_over_its_ceiling_loses_only_the_excess(client, store):
    """The ceiling is one number now, so the excess is tested with an explicit
    cap rather than by seeding 201 pages."""
    await store.persist_batch([page("shop.in", i, quality=1.0 - i * 0.05) for i in range(8)])

    agent = RuleEnforcerAgent(client, load_agent("01_frontier_agent").get_settings())
    domains, purged = await agent.enforce_page_cap(cap=5, dry_run=True)

    assert domains == 1
    assert purged == 3
    assert frontier.get_host_hard_limit("shop.in") == 200


@pytest.mark.asyncio
async def test_a_government_host_is_not_capped_at_five(client, store):
    """Eight pages is well under the uniform ceiling, whatever the suffix."""
    await store.persist_batch([page("big.gov.in", i) for i in range(8)])

    agent = RuleEnforcerAgent(client, load_agent("01_frontier_agent").get_settings())
    domains, purged = await agent.enforce_page_cap(dry_run=True)

    assert domains == 0
    assert purged == 0
    assert frontier.get_host_hard_limit("big.gov.in") == 200
