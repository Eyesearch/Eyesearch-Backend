"""The three integrations from the master specification."""

from __future__ import annotations

from app.agents import load_agent

parser = load_agent("03_parser_agent")
indexer = load_agent("05_indexer_agent")
frontier = load_agent("01_frontier_agent")

MAX_SNIPPET = indexer.MAX_SNIPPET_LENGTH
MIN_SNIPPET = indexer.MIN_SNIPPET_LENGTH


# ---------------------------------------------------------------------------
# Frontier admission
# ---------------------------------------------------------------------------


def test_the_frontier_refuses_a_token_url_before_fetching_it():
    """A rejected URL must not cost a queue slot or a network byte."""
    assert frontier.is_worthy_page("https://bank.example/reset-pw?token=123", 0) is False


def test_the_frontier_refuses_a_pagination_loop():
    assert frontier.is_worthy_page("https://shop.example/list?page=9", 0) is False


def test_the_frontier_admits_a_login_gateway():
    assert frontier.is_worthy_page("https://onlinesbi.sbi/login.htm", 12) is True


def test_the_frontier_refuses_past_the_cap():
    assert frontier.is_worthy_page("https://paytm.com/blog/article", 25) is False


# ---------------------------------------------------------------------------
# AI resale snippet
# ---------------------------------------------------------------------------


def test_the_capsule_is_capped_at_three_hundred_characters():
    body = "Karnataka One is the state portal for citizen services. " * 20

    assert len(indexer.format_ai_snippet(body)) <= MAX_SNIPPET


def test_a_sentence_end_is_preferred_to_a_mid_clause_cut():
    body = ("Karnataka One is the state portal for citizen services. " * 8).strip()
    snippet = indexer.format_ai_snippet(body)

    assert snippet.endswith(".") or snippet.endswith("...")
    assert not snippet.endswith(" ...")


def test_a_cookie_banner_is_dropped():
    banner = "We use cookies to improve your experience on this site and others."

    assert indexer.format_ai_snippet(banner) == ""


def test_a_label_is_not_a_capsule():
    """"Click here to login" is not knowledge."""
    assert indexer.format_ai_snippet("Click here to login") == ""


def test_the_meta_description_wins_over_the_body():
    meta = "The official portal for Karnataka citizen services and payments."
    body = "Some other paragraph entirely that is also long enough to qualify."

    assert indexer.format_ai_snippet(meta, None, body) == meta


def test_a_footer_of_legal_noise_is_dropped():
    footer = "All rights reserved. Copyright © 2026 Some Company Private Limited."

    assert indexer.format_ai_snippet(footer) == ""


def test_the_yield_per_domain_is_seven_and_a_half_thousand_characters():
    """25 pages x 300 characters is the number the resale case rests on."""
    assert 25 * MAX_SNIPPET == 7500


# ---------------------------------------------------------------------------
# The one display title
# ---------------------------------------------------------------------------


def test_the_card_title_is_brand_led_and_bounded():
    title = parser.format_company_bounded_title(
        "Citizen Registration & Scheme Portal Services India", "Seva Sindhu"
    )

    assert title.startswith("Seva Sindhu: ")
    assert len(title) <= 50
    assert "&..." not in title


def test_the_snippet_floor_is_sixty():
    assert MIN_SNIPPET == 60


# ---------------------------------------------------------------------------
# The contamination shield
# ---------------------------------------------------------------------------


def test_a_selling_preamble_is_stripped():
    text = (
        "Welcome to the official website of Karnataka One. It offers certificates, payments "
        "and applications for citizens across the state."
    )

    assert indexer.format_ai_snippet(text).startswith("Karnataka One.")


def test_marketing_hyperbole_is_stripped():
    text = "India's No.1 trusted portal for citizen services. Apply for certificates and pay bills online."

    assert not indexer.format_ai_snippet(text).startswith("India's")


def test_a_breadcrumb_trail_is_not_a_snippet():
    assert indexer.format_ai_snippet("Home > Services > Overview > Detail") == ""


def test_a_kannada_sentence_ending_is_respected():
    """Indic scripts end a sentence with a danda, not a full stop."""
    text = (
        "ಕರ್ನಾಟಕ ಸರ್ಕಾರವು ನಾಗರಿಕ ಸೇವೆಗಳನ್ನು ಒದಗಿಸುತ್ತದೆ. ಇದು ಅಧಿಕೃತ ಪೋರ್ಟಲ್ ಆಗಿದೆ. "
        "ಹೆಚ್ಚಿನ ಮಾಹಿತಿಗಾಗಿ ಇಲ್ಲಿ ನೋಡಿ ಮತ್ತು ಅರ್ಜಿ ಸಲ್ಲಿಸಿ. " * 3
    )
    snippet = indexer.format_ai_snippet(text)

    assert len(snippet) <= MAX_SNIPPET
    assert snippet.endswith(".") or snippet.endswith("।") or snippet.endswith("...")


def test_a_forced_cut_does_not_strand_a_halant():
    """A virama with no consonant after it is a broken glyph."""
    import unicodedata

    # A long run with no sentence end, so the word-boundary path is taken.
    text = "ಕರ್ನಾಟಕ ಸರ್ಕಾರದ ಆರೋಗ್ಯ ಇಲಾಖೆ " * 20
    snippet = indexer.format_ai_snippet(text)
    body = snippet[:-3] if snippet.endswith("...") else snippet

    assert unicodedata.category(body[-1]) != "Mn"
