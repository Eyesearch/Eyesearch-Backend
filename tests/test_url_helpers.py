"""The shared URL helpers: input is data, so they return None instead of raising."""

from __future__ import annotations

import pytest

from app.engine.urls import safe_port, safe_urljoin, safe_urlparse, safe_urlsplit

MALFORMED = "http://[::1/ malformed"


def test_safe_urlparse_returns_none_for_a_broken_authority():
    assert safe_urlparse(MALFORMED) is None
    assert safe_urlparse("https://karnataka.gov.in/a").netloc == "karnataka.gov.in"


def test_safe_urlsplit_returns_none_for_a_broken_authority():
    assert safe_urlsplit(MALFORMED) is None
    assert safe_urlsplit("https://karnataka.gov.in/a").path == "/a"


def test_safe_urljoin_returns_none_for_an_unclosed_ipv6_literal():
    """This is the exact shape that raised in the parser."""
    assert safe_urljoin("https://karnataka.gov.in/", "http://[::1/x") is None


def test_safe_urljoin_resolves_normal_hrefs():
    assert safe_urljoin("https://karnataka.gov.in/", "/notices") == (
        "https://karnataka.gov.in/notices"
    )
    assert safe_urljoin("https://karnataka.gov.in/", "https://iisc.ac.in/x") == (
        "https://iisc.ac.in/x"
    )


@pytest.mark.parametrize("value", [None, 42, b"bytes"])
def test_the_helpers_tolerate_non_strings(value):
    assert safe_urlparse(value) is None
    assert safe_urlsplit(value) is None
    assert safe_urljoin("https://a.gov.in/", value) is None
    assert safe_port(None) is None


def test_safe_port_reads_a_valid_port():
    assert safe_port(safe_urlparse("http://test.in:8080/a")) == 8080
    assert safe_port(safe_urlparse("https://test.in/a")) is None


def test_safe_port_is_none_for_an_uncastable_port():
    """`parts.port` raises on access, not at parse time."""
    parts = safe_urlparse("http://example.com:\xa0https://other.com")
    assert parts is not None, "the URL parses; only the port access fails"
    assert safe_port(parts) is None


def test_the_frontier_exports_the_shared_helper():
    from app.agents import load_agent

    frontier = load_agent("01_frontier_agent")
    assert frontier.safe_urlparse is safe_urlparse
