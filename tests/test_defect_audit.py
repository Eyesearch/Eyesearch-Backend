"""Contract breaches, their owning agent, and the sitelink extractor."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from app.agents import load_agent

_king = load_agent("14_king_orchestrator_agent")
DefectAuditor = _king.DefectAuditor
classify_row = _king.classify_row
mark_ratio = _king.mark_ratio
_parser = load_agent("03_parser_agent")
classify_label = _parser.classify_label
extract_sitelinks = _parser.extract_sitelinks

KANNADA_REAL = (
    "ಕರ್ನಾಟಕ ಆರೋಗ್ಯ ಇಲಾಖೆ ಜಿಲ್ಲಾ ಅಧಿಸೂಚನೆ ಸರ್ಕಾರ ಯೋಜನೆ ಪಾಸ್ಪೋರ್ಟ್ ರೈಲ್ವೆ ಪರೀಕ್ಷೆ ಫಲಿತಾಂಶ"
) * 2
KANNADA_STRIPPED = (
    "ಕರನಟಕ ಆರಗಯ ಇಲಾಖೆ ಜಲಲ ಅಧಸಚನ ಸರಕರ ಯಜನ ಪಸಪರಟ ರಲವ ಪರಕಷ ಫಲತಂಶ"
) * 2


def defects(**kwargs) -> list[str]:
    base = {
        "page_id": 1,
        "url": "https://a.in/x",
        "brand_name": "Karnataka",
        "body_text": "a well formed body with plenty of words in it for the audit to pass",
        "language": "en",
        "ceiling": 200,
    }
    base.update(kwargs)
    return [defect.kind for defect in classify_row(**base)]


def test_a_clean_row_has_no_defects():
    assert defects() == []


def test_a_missing_brand_is_the_metadata_extractor_s_doing():
    found = classify_row(
        page_id=1, url="https://a.in/x", brand_name="",
        body_text="a body with enough words in it to pass the thin content rule cleanly today",
        language="en", ceiling=200,
    )
    assert [d.kind for d in found] == ["empty_brand"]
    assert found[0].owner == "06_metadata_extractor"


def test_an_over_long_snippet_belongs_to_the_snippet_step():
    found = classify_row(
        page_id=1, url="https://a.in/x", brand_name="A", body_text="word " * 60,
        language="en", ceiling=200,
    )
    assert "snippet_too_long" in [d.kind for d in found]
    assert next(d.owner for d in found if d.kind == "snippet_too_long") == "10_snippet"


def test_thin_content_belongs_to_the_quality_guard():
    found = classify_row(
        page_id=1, url="https://a.in/x", brand_name="A", body_text="two words",
        language="en", ceiling=200,
    )
    assert "thin_content" in [d.kind for d in found]
    assert next(d.owner for d in found if d.kind == "thin_content") == "09_quality_guard"


def test_stripped_vowel_signs_belong_to_the_tokeniser():
    found = classify_row(
        page_id=1, url="https://a.in/x", brand_name="A", body_text=KANNADA_STRIPPED,
        language="kn", ceiling=2000,
    )
    assert "matras_stripped" in [d.kind for d in found]
    assert next(d.owner for d in found if d.kind == "matras_stripped") == "11_tokenizer"


def test_real_kannada_is_not_flagged():
    found = classify_row(
        page_id=1, url="https://a.in/x", brand_name="A", body_text=KANNADA_REAL,
        language="kn", ceiling=2000,
    )
    assert "matras_stripped" not in [d.kind for d in found]


def test_the_mark_share_separates_the_two():
    assert mark_ratio(KANNADA_STRIPPED) < 0.15
    assert mark_ratio(KANNADA_REAL) > 0.15


def test_an_english_page_is_never_flagged_for_missing_marks():
    found = classify_row(
        page_id=1, url="https://a.in/x", brand_name="A",
        body_text="a long enough english body with plenty of words in it to be content",
        language="en", ceiling=2000,
    )
    assert "matras_stripped" not in found


@pytest.mark.asyncio
async def test_the_auditor_counts_and_attributes(client, settings):
    from app.engine.store import IndexStore, PageDocument

    store = IndexStore(client, settings)
    await store.persist_batch(
        [
            PageDocument(
                url="https://a.gov.in/ok", domain="a.gov.in", title="Fine", description="d",
                body_text="karnataka health district notification office government scheme " * 3,
                digest="da-1", brand_name="Karnataka", language="en",
            ),
            PageDocument(
                url="https://b.gov.in/nobrand", domain="b.gov.in", title="No brand", description="d",
                body_text="karnataka health district notification office government scheme " * 3,
                digest="da-2", brand_name="", language="en",
            ),
        ]
    )

    report = await DefectAuditor(client, settings).scan()

    assert report.scanned == 2
    assert report.by_owner().get("06_metadata_extractor") == 1


# ---------------------------------------------------------------------------
# Sitelinks
# ---------------------------------------------------------------------------


def test_action_links_are_picked_out_of_a_nav_dump():
    html = """
    <a href="/apply">Apply Online</a>
    <a href="/track">Track Status</a>
    <a href="/contact">Contact Us</a>
    <a href="/about">About Us</a>
    <a href="/privacy">Privacy Policy</a>
    """
    links = extract_sitelinks(BeautifulSoup(html, "html.parser"), "https://karnataka.gov.in/dept/")

    assert [link.intent for link in links] == ["apply", "track", "contact"]
    assert links[0].url == "https://karnataka.gov.in/apply"


def test_a_sentence_is_not_a_link_label():
    assert classify_label("This is a very long anchor label that is really a sentence") == ""
    assert classify_label("Contact Us") == "contact"


def test_one_link_per_intent():
    html = '<a href="/a">Contact Us</a><a href="/b">Contact</a>'
    links = extract_sitelinks(BeautifulSoup(html, "html.parser"), "https://a.in/")

    assert len(links) == 1


def test_a_mailto_link_is_not_a_sitelink():
    html = '<a href="mailto:x@y.z">Contact Us</a>'
    assert extract_sitelinks(BeautifulSoup(html, "html.parser"), "https://a.in/") == []
