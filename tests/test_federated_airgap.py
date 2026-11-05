"""The air gap: nothing external reaches the store, the queue or the frontier."""

from __future__ import annotations

import pytest

from app.agents import load_agent

sanitizer = load_agent("44_cross_source_sanitizer")
trapper = load_agent("43_external_seed_trapper")
brands = load_agent("45_live_entity_extractor")


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def test_the_fleet_lives_behind_its_own_package():
    from app.agents import federated

    for name in (
        "FederatedHorizonAgent",
        "FreeKnowledgeCourierAgent",
        "ExternalSeedTrapperAgent",
        "CrossSourceSanitizerAgent",
        "LiveEntityExtractorAgent",
        "AsyncParallelDispatcherAgent",
    ):
        assert hasattr(federated, name), name


def test_the_old_numbered_names_still_resolve():
    """A rename that breaks every caller is a rename that gets reverted."""
    for old in (
        "41_federated_horizon_agent",
        "42_free_knowledge_courier",
        "43_external_seed_trapper",
        "44_cross_source_sanitizer",
        "45_live_entity_extractor",
        "46_async_parallel_dispatcher",
    ):
        assert "federated" in load_agent(old).__name__


# ---------------------------------------------------------------------------
# The air gap itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_trapper_never_accepts_anything(client):
    """Its old job was to enqueue external URLs; now it only counts."""
    agent = trapper.ExternalSeedTrapperAgent()
    report = await agent.trap(
        ["https://example.com/a", "https://karnataka.gov.in/x"]
    )

    assert report.offered == 2
    assert report.accepted == 0
    assert report.rejected.get("air_gap") == 2

    queued = await client.execute("SELECT COUNT(*) FROM crawl_queue")
    assert int(queued.scalar(0) or 0) == 0


@pytest.mark.asyncio
async def test_a_frontier_handed_in_is_ignored(client):
    """Passing one must not give the class a way to reach it."""

    class Frontier:
        async def add(self, *args, **kwargs):
            raise AssertionError("the trapper reached the frontier")

    agent = trapper.ExternalSeedTrapperAgent(frontier=Frontier())
    await agent.trap(["https://example.com/a"])

    assert agent._frontier is None


@pytest.mark.asyncio
async def test_a_federated_card_writes_nothing(client, store):
    agent = sanitizer.CrossSourceSanitizerAgent()
    agent.cards(
        [
            {"title": "A", "url": "https://example.com/a", "snippet": "text"},
            {"title": "B", "url": "https://example.com/b", "snippet": "more"},
        ]
    )

    pages = await client.execute("SELECT COUNT(*) FROM pages")
    queued = await client.execute("SELECT COUNT(*) FROM crawl_queue")
    assert int(pages.scalar(0) or 0) == 0
    assert int(queued.scalar(0) or 0) == 0


# ---------------------------------------------------------------------------
# Display rules
# ---------------------------------------------------------------------------


def test_a_brand_is_capped_at_fifty_characters():
    agent = brands.LiveEntityExtractorAgent()
    brand = agent.brand(title="x" * 200 + " | " + "y" * 200, url="https://example.com")

    assert len(brand) <= brands.BRAND_CHAR_CAP == 50


def test_a_short_brand_is_not_rejected():
    agent = brands.LiveEntityExtractorAgent()

    for name in ("NIC", "SBI", "ISRO"):
        assert agent.brand(title=name, url="https://example.com") == name


def test_the_brand_follows_the_domain_not_the_page_name():
    agent = brands.LiveEntityExtractorAgent()

    assert agent.brand(
        title="Personal Banking - Karnataka Bank", url="https://karnatakabank.com/x"
    ) == "Karnataka Bank"


def test_a_generic_tail_falls_back_to_the_domain():
    agent = brands.LiveEntityExtractorAgent()

    assert agent.brand(
        title="District Health Notifications | Official Website",
        url="https://karnataka.gov.in/x",
    ) == "Karnataka Gov"


def test_a_domain_is_derived_readably():
    assert brands.derive_brand_from_domain("karnataka.gov.in") == "Karnataka Gov"
    assert brands.derive_brand_from_domain("nic.in") == "Nic"


def test_the_two_snippets_and_the_flag():
    card = sanitizer.CrossSourceSanitizerAgent().card(
        title="T", url="https://example.com", snippet="word " * 200
    )

    assert len(card.snippet_short) <= sanitizer.SNIPPET_SHORT_CHARS == 300
    assert len(card.snippet_full) <= sanitizer.SNIPPET_FULL_CHARS == 1200
    assert card.has_more is True


def test_a_short_snippet_has_no_more():
    card = sanitizer.CrossSourceSanitizerAgent().card(
        title="T", url="https://example.com", snippet="a short notice"
    )

    assert card.has_more is False
    assert card.snippet_full == card.snippet_short


def test_a_title_is_capped_without_splitting_a_word():
    card = sanitizer.CrossSourceSanitizerAgent().card(
        title="word " * 60, url="https://example.com", snippet="x"
    )

    assert len(card.title) <= sanitizer.TITLE_CHAR_CAP
    assert not card.title.endswith("wor")


def test_markup_emoji_and_entities_are_cleaned():
    card = sanitizer.CrossSourceSanitizerAgent().card(
        title="Result",
        url="https://example.com/x?utm_source=a&fbclid=b",
        snippet="😀 <b>Bold</b> &amp; clean &#39;quoted&#39; Sponsored",
    )

    assert card.url == "https://example.com/x"
    assert "😀" not in card.snippet_short
    assert "<b>" not in card.snippet_short
    assert "&amp;" not in card.snippet_short
    assert "Sponsored" not in card.snippet_short
    assert "&" in card.snippet_short


def test_the_rupee_sign_and_the_en_dash_survive():
    """They are typography, not decoration."""
    card = sanitizer.CrossSourceSanitizerAgent().card(
        title="Fee", url="https://example.com", snippet="Fee is ₹500 – payable online"
    )

    assert "₹" in card.snippet_short
    assert "–" in card.snippet_short
