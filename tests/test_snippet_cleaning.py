"""Display-snippet cleaning. Display only: ranking and stored text are untouched."""

from __future__ import annotations

from app.services.search_service import clean_display_snippet


def test_accessibility_toolbar_is_removed():
    cleaned = clean_display_snippet(
        "Skip to Main Content Screen Reader Access Virtual Assistance By NIC "
        "Department of Health publishes notifications."
    )
    assert "Skip to Main Content" not in cleaned
    assert "Screen Reader Access" not in cleaned
    assert "Virtual Assistance By NIC" not in cleaned
    assert "Department of Health publishes notifications." in cleaned


def test_the_nic_chatbot_greeting_is_removed():
    cleaned = clean_display_snippet(
        "I am still learning. Please forgive me District offices open 9am to 5pm."
    )
    assert "still learning" not in cleaned
    assert cleaned == "District offices open 9am to 5pm."


def test_a_repeated_title_is_stripped_from_the_start():
    cleaned = clean_display_snippet(
        "Department of Health Department of Health The department publishes notifications.",
        title="Department of Health",
    )
    assert cleaned == "The department publishes notifications."


def test_a_title_elsewhere_in_the_text_is_kept():
    cleaned = clean_display_snippet(
        "Notice: Department of Health recruitment", title="Department of Health"
    )
    assert cleaned == "Notice: Department of Health recruitment"


def test_the_language_picker_does_not_eat_the_next_sentence():
    """A loose pattern here once turned '... certificate online.' into 'ficate online.'"""
    cleaned = clean_display_snippet(
        "Select Language English Hindi Kannada Apply for a certificate online."
    )
    assert cleaned == "Apply for a certificate online."


def test_plain_text_is_left_alone():
    text = "The department publishes notifications for district offices."
    assert clean_display_snippet(text) == text


def test_an_empty_snippet_stays_empty():
    assert clean_display_snippet("") == ""
    assert clean_display_snippet("   ") == ""


def test_a_snippet_of_only_boilerplate_becomes_empty():
    assert clean_display_snippet("Skip to Main Content Screen Reader Access") == ""


def test_the_snippet_is_bounded():
    assert len(clean_display_snippet("word " * 200)) <= 300
