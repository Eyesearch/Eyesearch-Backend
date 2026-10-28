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


# ---------------------------------------------------------------------------
# NIC accessibility toolbar and civic social rows
# ---------------------------------------------------------------------------


def test_the_screen_reader_language_toolbar_is_removed():
    cleaned = clean_display_snippet(
        "Screen Reader Hindi (हिन्दी) Department of Health publishes notices."
    )
    assert cleaned == "Department of Health publishes notices."


def test_a_dravidian_language_toolbar_is_removed():
    cleaned = clean_display_snippet(
        "Screen Reader Kannada (ಕನ್ನಡ) District office timings."
    )
    assert cleaned == "District office timings."


def test_a_sentence_about_screen_reader_support_survives():
    """The supplied `[A-Za-z]+` version ate the first two words of this."""
    text = "Screen Reader support is available at the helpdesk counter."
    assert clean_display_snippet(text) == text


def test_a_civic_social_row_is_removed():
    assert clean_display_snippet(
        "Home Facebook X District office contact details for citizens."
    ) == "District office contact details for citizens."


def test_a_snippet_that_is_only_a_social_row_becomes_empty():
    assert clean_display_snippet("Facebook X") == ""


def test_the_whole_social_row_goes_not_just_the_first_two():
    """The supplied pattern left 'Instagram YouTube District office timings.'"""
    assert clean_display_snippet(
        "Facebook X Instagram YouTube District office timings."
    ) == "District office timings."


def test_social_links_mid_sentence_do_not_leave_a_hole():
    """Anchoring at the start is what stops 'Follow us on for updates'."""
    text = "Follow us on Facebook X for updates about pensions."
    assert clean_display_snippet(text) == text


def test_a_lone_x_in_a_sentence_is_not_treated_as_a_social_link():
    text = "Plan X is available at the counter."
    assert clean_display_snippet(text) == text


def test_a_social_row_behind_the_title_is_still_removed():
    """Stripping the title can expose boilerplate, so the patterns run again."""
    assert clean_display_snippet(
        "Department of Health Facebook X District office timings.",
        title="Department of Health",
    ) == "District office timings."
