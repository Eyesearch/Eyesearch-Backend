"""The 1,000-character body cap.

The cap is a storage guarantee: no page, however long, may push a row past the
budget. It is also a presentation guarantee: the cut lands on a word boundary,
so a card never ends mid-word.
"""

from __future__ import annotations

from app.agents import load_agent

parser_module = load_agent("03_parser_agent")

BODY_CHAR_CAP = parser_module.BODY_CHAR_CAP
ELLIPSIS = parser_module.ELLIPSIS
ParserAgent = parser_module.ParserAgent
truncate_body = parser_module.truncate_body


def test_the_cap_is_one_thousand_characters():
    assert BODY_CHAR_CAP == 1000


def test_short_text_is_returned_unchanged():
    text = "Karnataka is a state in south western India."
    assert truncate_body(text) == text


def test_long_text_is_capped_and_closed_with_an_ellipsis():
    text = " ".join(["karnataka"] * 500)

    result = truncate_body(text)

    assert len(result) <= BODY_CHAR_CAP
    assert result.endswith(ELLIPSIS)


def test_the_cut_lands_on_a_word_boundary():
    text = " ".join(["bengaluru"] * 500)

    result = truncate_body(text)
    final_word = result[: -len(ELLIPSIS)].rsplit(" ", 1)[-1]

    assert final_word == "bengaluru"


def test_a_single_unbroken_token_is_still_capped():
    result = truncate_body("x" * 5000)

    assert len(result) <= BODY_CHAR_CAP
    assert result.endswith(ELLIPSIS)


def test_whitespace_is_collapsed_before_measuring():
    text = "  karnataka \n\n\t portal   " + " ".join(["word"] * 400)

    result = truncate_body(text)

    assert "\n" not in result
    assert "  " not in result
    assert result.startswith("karnataka portal word")


def test_the_parser_caps_a_long_page():
    body = "<p>" + " ".join(["government"] * 900) + "</p>"
    html = f"<html><head><title>Karnataka Portal</title></head><body>{body}</body></html>"

    parsed = ParserAgent().parse("https://karnataka.gov.in/", html)

    assert parsed.ok is True
    assert len(parsed.body_text) <= BODY_CHAR_CAP
    assert parsed.char_count <= BODY_CHAR_CAP


def test_the_parser_strips_scripts_styles_and_navigation():
    html = """
    <html><head><title>Bengaluru Metro</title>
    <meta name="description" content="Timings and fares for the Purple Line."></head>
    <body>
      <nav>Home About Contact</nav>
      <script>var secret = "should not be indexed";</script>
      <style>.x { color: red }</style>
      <p>Purple Line services run from five in the morning until eleven at night.</p>
      <footer>Copyright notice</footer>
    </body></html>
    """

    parsed = ParserAgent().parse("https://english.bmrc.co.in/metro-timings", html)

    assert parsed.title == "Bengaluru Metro"
    assert parsed.description == "Timings and fares for the Purple Line."
    assert "should not be indexed" not in parsed.body_text
    assert "color: red" not in parsed.body_text
    assert "Purple Line services" in parsed.body_text


def test_an_empty_document_is_rejected_rather_than_capped():
    parsed = ParserAgent().parse("https://example.invalid/", "")

    assert parsed.ok is False
    assert parsed.reason == "empty_response"
