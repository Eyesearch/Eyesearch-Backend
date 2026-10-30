"""Every agent carries concrete logic, and the three gaps are closed."""

from __future__ import annotations

import ast
import glob
import os

import pytest

from app.agents import load_agent


def agent_files() -> list[str]:
    return sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "app", "agents", "[0-9]*.py")))


def test_there_are_fifty_agents():
    assert len(agent_files()) == 50


@pytest.mark.parametrize("path", agent_files(), ids=os.path.basename)
def test_every_agent_defines_something(path):
    """No agent may be a blank stub: each defines a class or a function, and
    says at the top what it is for."""
    tree = ast.parse(open(path, encoding="utf-8").read())

    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]

    assert classes or functions, f"{os.path.basename(path)} defines nothing"
    assert ast.get_docstring(tree), f"{os.path.basename(path)} has no docstring"


@pytest.mark.parametrize("path", agent_files(), ids=os.path.basename)
def test_every_agent_imports_cleanly(path):
    name = os.path.basename(path)[:-3]
    assert load_agent(name) is not None


# ---------------------------------------------------------------------------
# 49 — PII redaction
# ---------------------------------------------------------------------------

PiiRedactor = load_agent("49_privacy_mask_agent").PiiRedactor


def test_an_aadhaar_number_is_redacted():
    assert "2345" not in PiiRedactor().redact("Aadhaar 2345 6789 0123 on file")


def test_an_unspaced_aadhaar_number_is_redacted():
    assert "234567890123" not in PiiRedactor().redact("id 234567890123")


def test_an_email_address_is_redacted():
    assert "officer@" not in PiiRedactor().redact("write to officer@karnataka.gov.in")


def test_a_phone_number_is_kept_by_default():
    """A district helpline is published so people can call it. Blanking it out
    would remove the most useful thing on the page."""
    text = "Call 9876543210 for help"

    assert "9876543210" in PiiRedactor().redact(text)


def test_phone_redaction_is_available_when_asked_for():
    assert "9876543210" not in PiiRedactor(redact_phones=True).redact("Call 9876543210")


def test_an_order_number_is_not_mistaken_for_an_aadhaar():
    """An Aadhaar number never begins with 0 or 1."""
    text = "Order 1234 5678 9012 dispatched"

    assert "1234" in PiiRedactor().redact(text)


# ---------------------------------------------------------------------------
# 19 — cache invalidation through the agent
# ---------------------------------------------------------------------------


def test_the_cache_agent_forgets_a_written_page():
    agent = load_agent("19_fast_cache_agent").FastCacheAgent()
    agent.set(("q",), {"results": [{"url": "https://a.in/1"}]})

    assert agent.invalidate_urls(["https://a.in/1"]) == 1
    assert agent.get(("q",)) is None


# ---------------------------------------------------------------------------
# 06 — conditional requests
# ---------------------------------------------------------------------------

revalidation = load_agent("06_revalidation_agent")


def test_a_validator_becomes_a_conditional_header():
    headers = revalidation.conditional_headers('"abc"', "Wed, 21 Oct 2026 07:28:00 GMT")

    assert headers["If-None-Match"] == '"abc"'
    assert headers["If-Modified-Since"] == "Wed, 21 Oct 2026 07:28:00 GMT"


def test_no_validators_means_no_conditional_headers():
    assert revalidation.conditional_headers() == {}


def test_a_304_means_the_page_is_unchanged():
    assert revalidation.is_unchanged(304) is True
    assert revalidation.is_unchanged(200) is False


def test_validators_are_read_back_from_a_response():
    found = revalidation.validators_from({"ETag": '"abc"', "Last-Modified": "Wed, 21 Oct 2026"})

    assert found == {"etag": '"abc"', "last_modified": "Wed, 21 Oct 2026"}
