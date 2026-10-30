"""The four gates that keep polluted text out of the index."""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.engine.store import PageDocument

parser = load_agent("03_parser_agent")
brand_module = load_agent("10_brand_canonical_agent")

PAGE = """<html><body>
<article><p>Karnataka One is the state portal for citizen services.</p></article>
<button>Okay Cancel</button>
<div role="dialog">Translation Feedback CPGRAMS</div>
<div aria-hidden="true">Share Colour</div>
<video>Loading</video>
<p>Fees are Rs 250 for a certificate.</p>
</body></html>"""


# ---------------------------------------------------------------------------
# 1. Parser: interactive chrome never becomes prose
# ---------------------------------------------------------------------------


def test_button_labels_do_not_reach_the_body():
    body = parser.ParserAgent().parse("https://karnataka.gov.in/x", PAGE).body_text

    assert "Okay Cancel" not in body


def test_a_modal_is_decomposed():
    body = parser.ParserAgent().parse("https://karnataka.gov.in/x", PAGE).body_text

    assert "CPGRAMS" not in body


def test_an_aria_hidden_overlay_is_decomposed():
    body = parser.ParserAgent().parse("https://karnataka.gov.in/x", PAGE).body_text

    assert "Share Colour" not in body


def test_media_controls_are_stripped():
    html = "<html><body><p>Real sentence here. ⏪ ⏸️ ▶️ ⏩ 🔄 ➡️</p></body></html>"
    body = parser.ParserAgent().parse("https://a.example/x", html).body_text

    assert "⏪" not in body and "➡️" not in body


def test_a_rupee_sign_is_content_and_survives():
    """The symbol range must not eat currency."""
    html = "<html><body><p>The fee is ₹ 250 for this certificate.</p></body></html>"
    body = parser.ParserAgent().parse("https://a.example/x", html).body_text

    assert "₹" in body


# ---------------------------------------------------------------------------
# 2. Parser: the title never becomes a domain suffix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [".in", "India.gov.in", "com", "-::", "::"])
def test_a_bare_tld_never_becomes_the_title_context(raw):
    title = parser.format_company_bounded_title(raw, "India.gov", url="https://india.gov.in/")

    assert title == "India.gov: Official Portal"


def test_punctuation_scars_are_trimmed_from_a_brand():
    assert brand_module.sanitize_brand("-:: Rmrc Bbsr") == "Rmrc Bbsr"
    assert brand_module.sanitize_brand(":: NIV") == "NIV"


def test_a_local_dev_prefix_is_removed():
    assert brand_module.sanitize_brand("Local Dev Portal") == "Portal"


# ---------------------------------------------------------------------------
# 3. The caps hold everywhere
# ---------------------------------------------------------------------------


def test_the_parser_declares_the_architecture_caps():
    assert parser.TITLE_CHAR_CAP == 50
    assert parser.BRAND_CHAR_CAP == 35
    assert parser.DESCRIPTION_CHAR_CAP == 300


def test_the_store_truncates_no_matter_what_it_is_handed():
    """The last line of defence: a bug upstream cannot widen a column."""
    document = PageDocument(
        url="https://a.example/x",
        domain="a.example",
        title="T" * 200,
        description="D" * 900,
        body_text="body",
        digest="hyg-1",
        brand_name="B" * 200,
        action_title="A" * 200,
    )
    row = document.as_row()

    assert len(row[2]) == 50     # title
    assert len(row[3]) == 300    # description
    assert len(row[9]) == 35     # brand
    assert len(row[11]) == 50    # action title
