"""Curated brands, the 35-character limit, and the parsing fallbacks."""

from __future__ import annotations

import pytest

from app.agents import load_agent

module = load_agent("10_brand_canonical_agent")
curated_brand = module.curated_brand
trim_brand = module.trim_brand
MAX_BRAND_LENGTH = module.MAX_BRAND_LENGTH


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://karnataka.gov.in/x", "Government of Karnataka"),
        ("https://www.thehindu.com/news", "The Hindu"),
        ("https://sbi.co.in/web", "State Bank of India"),
        ("https://sci.gov.in/judgments", "Supreme Court of India"),
        ("https://indiankanoon.org/doc/1", "Indian Kanoon"),
    ],
)
def test_a_curated_host_gets_its_real_name(url, expected):
    assert curated_brand(url) == expected


def test_a_subdomain_inherits_the_parent_brand():
    """ksp.karnataka.gov.in is the police, not the state government."""
    assert curated_brand("https://ksp.karnataka.gov.in/a") == "Karnataka State Police"


def test_an_unknown_host_has_no_curated_name():
    assert curated_brand("https://unknownsite.com/x") == ""


def test_no_curated_name_is_longer_than_the_limit():
    for name in module.CURATED_BRANDS.values():
        assert len(name) <= MAX_BRAND_LENGTH, name


def test_fluff_is_stripped():
    assert trim_brand("Karnataka Bank - Official Website") == "Karnataka Bank"
    # "portal home" is on the fluff list, so it goes with the rest of it.
    assert trim_brand("Karnataka One Portal Home") == "Karnataka One"


def test_a_long_name_is_cut_at_a_word_boundary():
    trimmed = trim_brand("Some Very Long Official Website Name Here Today")

    assert len(trimmed) <= MAX_BRAND_LENGTH
    assert not trimmed.endswith(" ")
    assert "Some Very Long Official Website" in trimmed


def test_a_single_long_word_is_cut_at_the_limit():
    assert len(trim_brand("A" * 60)) == MAX_BRAND_LENGTH


def test_the_parsing_steps_still_follow_the_curated_map():
    """The list is first, not only: a page it does not know is still parsed."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<meta property="og:site_name" content="Some Local Shop">', "html.parser"
    )
    agent = module.BrandCanonicalAgent()

    assert agent.brand(soup, "https://somelocalshop.com/") == "Some Local Shop"
