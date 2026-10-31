"""The Indian domain shield and the hard per-host page ceilings."""

from __future__ import annotations

import pytest

from app.agents import load_agent

frontier = load_agent("01_frontier_agent")


@pytest.mark.parametrize(
    "url",
    [
        "https://karnataka.gov.in/x",
        "https://bengaluruurban.nic.in/y",
        "https://iitb.ac.in/z",
        "https://somewhere.co.in/a",
        "https://flipkart.com/b",
        "https://www.flipkart.com/c",
        "https://irctc.co.in/d",
    ],
)
def test_the_indian_web_is_allowed(url):
    assert frontier.is_allowed_indian_domain(url) is True


@pytest.mark.parametrize(
    "url",
    ["https://github.com/x", "https://example.com/y", "https://bbc.co.uk/z", "https://python.org/a"],
)
def test_a_foreign_host_is_refused(url):
    assert frontier.is_allowed_indian_domain(url) is False


def test_a_malformed_url_does_not_raise():
    for junk in ("", "not a url", "://", "https://"):
        assert frontier.is_allowed_indian_domain(junk) is False


@pytest.mark.parametrize(
    "url, limit",
    [
        ("https://karnataka.gov.in/x", 250),
        ("https://bengaluruurban.nic.in/y", 250),
        ("https://iitb.ac.in/z", 50),
        ("https://flipkart.com/a", 50),
        ("https://somewhere.in/b", 5),
        ("https://github.com/x", 0),
    ],
)
def test_each_tier_has_its_hard_ceiling(url, limit):
    assert frontier.get_host_hard_limit(url) == limit


def test_the_tiers_are_stops_not_ranges():
    assert (frontier.HARD_LIMIT_GOV, frontier.HARD_LIMIT_INSTITUTION) == (250, 50)
    assert (frontier.HARD_LIMIT_GENERAL, frontier.HARD_LIMIT_FOREIGN) == (5, 0)


def test_the_sql_mirrors_the_python_rule():
    """The queue's exclusion is generated from the same constants."""
    sql = frontier.host_limit_sql("s.domain")

    assert "250" in sql and "50" in sql and "5" in sql and "0" in sql
    assert "s.domain" in sql
