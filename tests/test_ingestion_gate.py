"""Frontier priority, outlink curation, the identity gate and crawl speed."""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.engine.config import Settings

frontier = load_agent("01_frontier_agent")
quality = load_agent("04_quality_agent")


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.india.gov.in/x",
        "https://isro.gov.in/y",
        "https://pib.gov.in/z",
        "https://uidai.gov.in/a",
        "https://karnataka.gov.in/b",
        "https://bengaluruurban.nic.in/c",
        "https://iitb.ac.in/d",
        "https://mit.edu/e",
    ],
)
def test_sovereign_and_academic_domains_are_first(url):
    """Band 0 comes from the suffix, not from a list of names."""
    assert frontier.classify_domain_priority(url) == 0


def test_an_ordinary_site_is_enterprise_tier():
    assert frontier.classify_domain_priority("https://someblog.com/post") == 2


def test_a_financial_host_is_utility_tier():
    """A bank is recognised by its shape, not by its name."""
    assert frontier.classify_domain_priority("https://hdfcbank.com/a") == 1
    assert frontier.classify_domain_priority("https://acme-bank.example/b") == 1


def test_a_referral_from_a_government_portal_lifts_a_provider():
    context = {"source_priority": 0, "source_host": "india.gov.in"}
    assert frontier.classify_domain_priority("https://serviceprovider.example/", context) == 1


def test_a_referral_does_not_rescue_a_spam_shaped_host():
    """The shape is the evidence; vouching for it does not change that."""
    context = {"source_priority": 0, "source_host": "india.gov.in"}
    assert (
        frontier.classify_domain_priority("https://best-cheap-deals-online-2026.com/x", context) == 99
    )


def test_a_generated_host_is_discarded():
    assert frontier.classify_domain_priority("https://site123456.com/x") == 99


# ---------------------------------------------------------------------------
# Outlink curation
# ---------------------------------------------------------------------------


def test_a_blog_does_not_get_to_refer_the_whole_web():
    """Two ordinary sites linking to each other is the random network this stops."""
    assert (
        frontier.in_crawl_scope("https://another-blog.com/post", "someblog.com", allow_global=True)
        is False
    )


def test_a_verified_target_is_followed_from_a_verified_source():
    assert frontier.in_crawl_scope("https://somebank.com/a", "karnataka.gov.in", allow_global=True) is True


def test_a_discarded_target_is_never_followed():
    assert (
        frontier.in_crawl_scope(
            "https://best-cheap-deals-online-2026.com/x", "karnataka.gov.in", allow_global=True
        )
        is False
    )


def test_a_government_site_may_refer_outwards():
    assert frontier.in_crawl_scope("https://random.com/x", "karnataka.gov.in", allow_global=True) is True


def test_a_site_may_always_link_to_itself():
    assert frontier.in_crawl_scope("https://someblog.com/other", "someblog.com", allow_global=True) is True


# ---------------------------------------------------------------------------
# Identity gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "domain, brand",
    [
        ("karnataka.gov.in", "Karnataka Health"),
        ("thehindu.com", "The Hindu"),
        ("indiatoday.in", "India Today"),
    ],
)
def test_a_brand_that_belongs_to_its_domain_passes(domain, brand):
    ok, reason = quality.verify_identity(domain, brand, "District Health")
    assert ok is True, reason


def test_an_empty_brand_is_rejected():
    ok, reason = quality.verify_identity("karnataka.gov.in", "", "District Health")
    assert ok is False and reason == "empty_brand"


def test_a_brand_from_another_site_is_rejected():
    ok, reason = quality.verify_identity("someblog.com", "Karnataka", "A title")
    assert ok is False and reason == "brand_not_from_domain"


def test_a_title_over_four_words_is_rejected():
    ok, reason = quality.verify_identity(
        "karnataka.gov.in", "Karnataka", "District Health Notifications For Every Office"
    )
    assert ok is False and reason == "title_too_long"


def test_a_title_borrowing_another_brand_is_rejected():
    ok, reason = quality.verify_identity("karnataka.gov.in", "Karnataka", "Best Amazon deals")
    assert ok is False and reason.startswith("title_borrows_brand")


def test_a_site_may_use_its_own_brand_in_a_title():
    ok, reason = quality.verify_identity("youtube.com", "YouTube", "YouTube Kids")
    assert ok is True, reason


# ---------------------------------------------------------------------------
# Crawl speed and the ceilings that must not move
# ---------------------------------------------------------------------------


def test_the_crawl_is_back_to_one_at_a_time():
    settings = Settings(_env_file=None)

    assert settings.crawl_concurrency == 1
    assert settings.crawl_batch_sleep_seconds == 10.0
    assert settings.crawl_empty_backoff_seconds == 20.0


def test_the_ceilings_are_unchanged():
    settings = Settings(_env_file=None)

    assert settings.max_pages_per_domain == 25
    assert settings.page_body_char_ceiling == 200
