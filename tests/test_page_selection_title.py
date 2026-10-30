"""Page selection and the 50-character card title."""

from __future__ import annotations

import pytest

from app.agents import load_agent

parser = load_agent("03_parser_agent")
is_worthy_page = parser.is_worthy_page
format_title = parser.format_company_bounded_title
MAX_TITLE_LENGTH = parser.MAX_TITLE_LENGTH


# ---------------------------------------------------------------------------
# Page selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, count, expected",
    [
        ("https://onlinesbi.sbi/portal/login.html", 10, True),
        ("https://paytm.com/signup", 4, True),
        ("https://sevasindhu.karnataka.gov.in/citizen/register", 15, True),
        ("https://ksp.karnataka.gov.in/services", 24, True),
        ("https://ksp.karnataka.gov.in/about", 25, False),
        ("https://somebank.com/reset-password?token=abc", 2, False),
        ("https://blog.site.com/tag/news", 1, False),
    ],
)
def test_a_page_is_kept_or_dropped_for_the_right_reason(url, count, expected):
    assert is_worthy_page(url, count) is expected


def test_the_cap_is_a_ceiling_not_a_target():
    """A small site with five real pages is complete at five."""
    assert is_worthy_page("https://small.example/about", 5) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://bank.example/logout",
        "https://bank.example/signout",
        "https://bank.example/forgot-password",
        "https://bank.example/reset-password?token=abc",
        "https://shop.example/list?page=4",
        "https://shop.example/page/7",
        "https://news.example/tag/karnataka",
        "https://news.example/feed/rss",
    ],
)
def test_session_and_loop_urls_are_never_stored(url):
    assert is_worthy_page(url, 0) is False


def test_a_shallow_clean_path_is_kept():
    assert is_worthy_page("https://bank.example/products", 0) is True


# ---------------------------------------------------------------------------
# The title
# ---------------------------------------------------------------------------


def test_the_brand_leads_the_title():
    """A search for the brand matches the first word."""
    assert format_title("Net Banking", "Canara Bank").startswith("Canara Bank: ")


def test_the_brand_is_said_once():
    """'SBI: SBI Personal Banking' is the same word twice."""
    title = format_title("SBI Personal Banking", "SBI")

    assert title.lower().count("sbi") == 1
    assert title == "SBI: Personal Banking"


def test_fluff_is_stripped():
    title = format_title("Welcome to the Official Website of Some Bank", "Some Bank")

    assert "welcome" not in title.lower()
    assert "official website" not in title.lower()


def test_a_long_title_is_cut_at_a_word_boundary_with_an_ellipsis():
    title = format_title("Citizen Registration & Application Services System Information", "Seva Sindhu")

    assert len(title) <= MAX_TITLE_LENGTH
    assert title.endswith("...")
    assert not title[:-3].endswith(" ")
    assert "Registratio..." not in title


def test_a_dangling_symbol_does_not_survive_the_cut():
    """'Registration &...' reads as a mistake; 'Registration...' does not."""
    title = format_title("Citizen Registration & Application Services System Information", "Seva Sindhu")

    assert "&..." not in title
    assert title == "Seva Sindhu: Citizen Registration..."


def test_no_padding_when_the_title_is_already_short():
    title = format_title("Net Banking", "Canara Bank")

    assert title == "Canara Bank: Net Banking"
    assert len(title) < MAX_TITLE_LENGTH


def test_every_title_respects_the_limit():
    for raw, brand in [
        ("A" * 80, "Brand"),
        ("Welcome to " + "word " * 30, "Brand"),
        ("Paytm: Secure UPI Payments, Bill Payments & Wallet Recharge", "Paytm"),
    ]:
        assert len(format_title(raw, brand)) <= MAX_TITLE_LENGTH
