"""Brand resolution: five steps, and never empty."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from app.agents import load_agent

BrandCanonicalAgent = load_agent("10_brand_canonical_agent").BrandCanonicalAgent
brand_from_domain = load_agent("10_brand_canonical_agent").brand_from_domain


def brand(html: str, url: str) -> str:
    return BrandCanonicalAgent().brand(BeautifulSoup(html, "html.parser"), url)


def test_open_graph_wins_first():
    html = '<meta property="og:site_name" content="India Today">'
    assert brand(html, "https://indiatoday.in/x") == "India Today"


def test_json_ld_is_the_second_choice():
    html = (
        '<script type="application/ld+json">'
        '{"@type": "Organization", "name": "Prabhat Khabar"}'
        "</script>"
    )
    assert brand(html, "https://prabhatkhabar.com/x") == "Prabhat Khabar"


def test_a_publisher_object_is_read_from_json_ld():
    html = (
        '<script type="application/ld+json">'
        '{"@type": "NewsArticle", "publisher": {"name": "The Hindu"}}'
        "</script>"
    )
    assert brand(html, "https://thehindu.com/x") == "The Hindu"


def test_twitter_site_is_the_third_choice():
    html = '<meta name="twitter:site" content="@the_hindu">'
    assert brand(html, "https://thehindu.com/x") == "The Hindu"


def test_the_title_tail_is_the_fourth_choice():
    """On a host the curated list does not know, the title tail is still the
    fourth thing tried."""
    html = "<title>District Health Notifications | District Health Department</title>"
    assert brand(html, "https://healthdept.example.gov.in/x") == "District Health Department"


def test_a_title_without_a_separator_is_not_mined_for_a_brand():
    """The whole title is the subject; there is no publisher tail to take."""
    html = "<title>District Health Notifications</title>"
    assert brand(html, "https://healthdept.example.gov.in/x") == "Example"


def test_the_domain_is_the_last_resort():
    assert brand("", "https://www.indiatoday.in/x") == "Indiatoday"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://karnataka.gov.in/x", "Karnataka"),
        # Short labels are acronyms, and upper-casing them is right.
        ("https://www.iitb.ac.in/x", "IITB"),
        ("https://thehindu.com/x", "Thehindu"),
    ],
)
def test_the_domain_fallback_is_never_empty(url, expected):
    assert brand_from_domain(url) == expected


def test_a_page_with_nothing_at_all_still_gets_a_brand():
    """Zero tolerance for an empty brand: a blank line on a card reads as broken."""
    result = BrandCanonicalAgent().brand(BeautifulSoup("<html></html>", "html.parser"), "https://x.in/y")

    assert result
