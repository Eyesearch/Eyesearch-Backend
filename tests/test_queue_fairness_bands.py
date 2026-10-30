"""Every legitimate domain shares one queue priority; spam still does not."""

from __future__ import annotations

import pytest

from app.agents import load_agent

frontier = load_agent("01_frontier_agent")

LEGITIMATE = [
    "https://karnataka.gov.in/x",
    "https://www.india.gov.in/y",
    "https://iitb.ac.in/z",
    "https://news18.com/a",
    "https://examplebank.com/b",
    "https://shop.example/c",
    "https://en.wikipedia.org/wiki/x",
]


@pytest.mark.parametrize("url", LEGITIMATE)
def test_every_legitimate_domain_is_queued_at_the_same_priority(url):
    """A government portal must not outrank a news site in the queue."""
    assert frontier.priority_of(url) == frontier.ENQUEUE_PRIORITY


def test_the_queue_priority_is_one():
    assert frontier.ENQUEUE_PRIORITY == 1


def test_the_band_still_distinguishes_what_it_is_for():
    """Equal priority, not equal treatment: the band still says what a site is."""
    assert frontier.classify_domain_priority("https://karnataka.gov.in/x") == 0
    assert frontier.classify_domain_priority("https://news18.com/a") == 2


@pytest.mark.parametrize(
    "url",
    ["https://best-cheap-deals-online-2026.com/x", "https://site123456.com/x"],
)
def test_spam_is_still_discarded(url):
    """Equalising priority must not admit the spam filter's rejects."""
    assert frontier.priority_of(url) == 99


def test_the_page_cap_is_unchanged():
    from app.engine.config import Settings

    assert Settings(_env_file=None).max_pages_per_domain == 25
