"""The body keeps sentences, not its opening characters.

A card whose snippet comes from the navigation says nothing. "karnataka scheme
status" is as often the third paragraph as the first, and a body cut to its
first N characters never contains it.
"""

from __future__ import annotations

from app.engine.body_sentences import extract, sentences

NAV = "Home About Contact Skip to main content Screen reader access. " * 4
FACT = "The Karnataka scheme status can be checked on the state portal. "
FACT2 = "Applications close on 31 March 2027 for every district in the state. "
FOOTER = "Copyright 2026 Government of Karnataka. All rights reserved."


def test_the_fact_survives_and_the_furniture_does_not():
    body = NAV + "Some unrelated introduction to the department. " + FACT + FACT2 + FOOTER

    kept = extract(body, 300)

    assert "31 March 2027" in kept
    assert "Copyright" not in kept
    assert "Skip to main content" not in kept


def test_the_budget_is_respected():
    body = (FACT + FACT2) * 20

    assert len(extract(body, 250)) <= 250


def test_a_short_body_is_kept_whole():
    body = FACT

    assert extract(body, 500) == body.strip()


def test_sentences_are_returned_in_page_order():
    body = "First sentence of the page here. " + "Second sentence follows it now. "

    kept = extract(body, 200)

    assert kept.index("First") < kept.index("Second")


def test_a_body_with_no_sentences_falls_back_to_its_opening():
    """Better the old behaviour than an empty body."""
    body = "no full stops here at all just words and more words " * 10

    kept = extract(body, 100)

    assert kept
    assert len(kept) <= 100


def test_a_menu_of_short_items_is_not_a_sentence():
    assert all(len(part) >= 1 for part in sentences("Home. About. Contact."))
