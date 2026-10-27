"""Frontier sitemap harvesting and the India-only seed-scope contract."""

from __future__ import annotations

import httpx

from app.agents import load_agent
from app.engine.config import Settings

frontier_module = load_agent("01_frontier_agent")
FrontierAgent = frontier_module.FrontierAgent
SEED_URLS = frontier_module.SEED_URLS
domain_of = frontier_module.domain_of
is_authoritative = frontier_module.is_authoritative


def test_all_seed_domains_remain_in_the_intended_indian_scope():
    for url in SEED_URLS:
        domain = domain_of(url)
        assert domain
        assert is_authoritative(domain), f"out-of-scope seed: {url} -> {domain}"


async def test_sitemap_harvest_collects_urls_from_multiple_domains(store, settings):
    """A usable first sitemap must not prevent later discovered domains being harvested."""

    def handler(request: httpx.Request) -> httpx.Response:
        domain = request.url.host
        return httpx.Response(
            200,
            text=(
                "<urlset><url><loc>"
                f"https://{domain}/about"
                "</loc></url></urlset>"
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        frontier = FrontierAgent(store, settings, client=client)
        queued = await frontier.harvest_sitemap(
            ("india.gov.in", "karnataka.gov.in"),
            force=True,
        )

    assert queued == 2
    assert await store.queue_depth() == 2
