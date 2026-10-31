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
    "url",
    [
        "https://karnataka.gov.in/x",
        "https://bengaluruurban.nic.in/y",
        "https://iitb.ac.in/z",
        "https://flipkart.com/a",
        "https://somewhere.in/b",
        "https://ksrtc.in/c",
    ],
)
def test_every_indian_host_has_the_same_ceiling(url):
    """One ceiling for the Indian web: a transport corporation deserves the
    same depth as a ministry, and content decides what fills it."""
    assert frontier.get_host_hard_limit(url) == 200


def test_a_foreign_host_has_no_ceiling_at_all():
    assert frontier.get_host_hard_limit("https://github.com/x") == 0


def test_the_ceiling_is_one_number_and_zero():
    assert frontier.UNIFORM_HOST_CAP == 200
    assert frontier.HARD_LIMIT_FOREIGN == 0


def test_the_sql_mirrors_the_python_rule():
    """The queue's exclusion is generated from the same constants."""
    sql = frontier.host_limit_sql("s.domain")

    assert "200" in sql and "ELSE 0" in sql
    assert "s.domain" in sql


def test_a_state_transport_site_is_not_capped_like_a_mailbox():
    """The tiers are gone because they encoded a false assumption: a transport
    corporation is a public service with hundreds of useful pages, not five."""
    for url in ("https://ksrtc.in/x", "https://bescom.karnataka.gov.in/y", "https://bbmp.gov.in/z"):
        assert frontier.get_host_hard_limit(url) == frontier.UNIFORM_HOST_CAP == 200
