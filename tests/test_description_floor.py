"""Maximums only: a short clean description is kept, not replaced."""

from __future__ import annotations

import pytest

from app.agents import load_agent

quality = load_agent("04_quality_agent")
parser_module = load_agent("03_parser_agent")
clean_description = quality.clean_description
sanitize_text = quality.sanitize_text


def test_the_body_cap_is_two_kilobytes():
    assert parser_module.BODY_CHAR_CAP == 2000


def test_the_other_ceilings_are_unchanged():
    assert parser_module.TITLE_CHAR_CAP == 50
    assert parser_module.BRAND_CHAR_CAP == 35
    assert parser_module.DESCRIPTION_CHAR_CAP == 300


@pytest.mark.parametrize(
    "text",
    [
        "Apply for a birth certificate.",          # 32 characters
        "Karnataka One portal",                     # 21
        "Pay your bill online here.",               # 26
    ],
)
def test_a_short_clean_description_is_kept_exactly(text):
    """No minimum length. A clean 21-character description is the answer."""
    assert clean_description(text) == text


def test_a_short_description_never_borrows_from_the_body():
    body = "The department publishes notifications for district offices across the state."

    assert clean_description("Pay your bill online.", body) == "Pay your bill online."


def test_the_body_is_read_only_when_the_description_is_empty():
    body = "The department publishes notifications for district offices across the state of Karnataka."
    result = clean_description("", body, "Portal")

    assert result.startswith("The department publishes")
    assert "official portal information" not in result


def test_the_title_carries_the_last_resort():
    assert clean_description("", "", "Karnataka One") == "Karnataka One — official portal information"


def test_a_cookie_banner_is_treated_as_empty():
    body = "Karnataka One publishes certificates and payments for citizens across the state."

    assert clean_description("We use cookies to improve your experience.", body).startswith(
        "Karnataka One publishes"
    )


def test_entities_are_decoded():
    assert sanitize_text("Karnataka &amp; its districts") == "Karnataka & its districts"


def test_markup_is_stripped():
    assert sanitize_text("<b>Portal</b> for services") == "Portal for services"


def test_repeated_punctuation_collapses():
    assert sanitize_text("Really!!! Sure???") == "Really! Sure?"


def test_decoration_is_stripped_but_the_rupee_sign_survives():
    cleaned = sanitize_text("Fee is ₹ 250 ⏪ ▶️")

    assert "⏪" not in cleaned
    assert "₹" in cleaned


def test_a_description_is_capped_not_rejected():
    long = "word " * 200
    result = clean_description(long)

    assert len(result) <= parser_module.DESCRIPTION_CHAR_CAP
    assert result != ""


def test_the_page_density_gate_still_exists():
    """80 characters is about the whole page being empty, not about a field."""
    assert quality.MIN_BODY_CHARS == 80
