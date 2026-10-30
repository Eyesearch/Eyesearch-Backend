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
        "https://www.youtube.com/watch",
        "https://en.wikipedia.org/wiki/x",
        "https://karnataka.gov.in/b",
        "https://iitb.ac.in/c",
    ],
)
def test_named_and_civic_domains_are_first(url):
    assert frontier.priority_of(url) == 0


def test_an_ordinary_site_is_last():
    assert frontier.priority_of("https://someblog.com/post") == 2


def test_the_indian_web_sits_between_them():
    assert frontier.priority_of("https://shop.in/x") == 1


# ---------------------------------------------------------------------------
# Outlink curation
# ---------------------------------------------------------------------------


def test_a_blog_does_not_get_to_refer_the_whole_web():
    """A low-utility site contributes its own pages, not a tour of its links."""
    assert (
        frontier.in_crawl_scope("https://another-blog.com/post", "someblog.com", allow_global=True)
        is False
    )


def test_a_high_utility_target_is_followed_from_anywhere():
    assert frontier.in_crawl_scope("https://youtube.com/x", "someblog.com", allow_global=True) is True


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
