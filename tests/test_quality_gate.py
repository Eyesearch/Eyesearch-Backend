"""The quality gate: the 80-character floor and SHA-256 deduplication."""

from __future__ import annotations

import hashlib

from app.agents import load_agent

quality = load_agent("04_quality_agent")
from tests.conftest import make_document

quality_module = load_agent("04_quality_agent")

MIN_BODY_CHARS = quality_module.MIN_BODY_CHARS
QualityAgent = quality_module.QualityAgent
digest_of = quality_module.digest_of


def test_the_floor_is_eighty_characters():
    assert MIN_BODY_CHARS == 80


def test_thin_body_text_is_rejected():
    document = make_document(body_text="Karnataka portal.")  # 17 characters

    verdict = QualityAgent().check(document)

    assert verdict.ok is False
    assert verdict.reason == "low_text_density"


def test_body_text_at_the_floor_is_accepted():
    body = "a" * 79 + "b"  # exactly 80 characters, one token
    document = make_document(body_text=body, title="Karnataka State Portal")

    verdict = QualityAgent().check(document)

    assert verdict.ok is True
    assert verdict.reason == ""


def test_the_digest_covers_the_page_not_only_its_body():
    """It used to be sha256(body). Once the body is capped at 200 characters,
    two unrelated pages sharing a template preamble produced one fingerprint and
    the second was dropped as a duplicate. The URL is part of a page's identity.
    """
    body = "Karnataka is a state in south western India, and its capital is Bengaluru, the largest city in the state."
    document = make_document(url="https://a.gov.in/x", body_text=body)

    verdict = QualityAgent().check(document)

    assert verdict.digest == quality.page_digest("https://a.gov.in/x", body)
    assert verdict.digest != hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_whitespace_does_not_change_the_digest():
    first = make_document(body_text="one  two\n\nthree " + "x" * 80)
    second = make_document(body_text="one two three " + "x" * 80)

    gate = QualityAgent()

    assert gate.check(first).digest == gate.check(second).digest


def test_the_same_url_twice_in_a_batch_is_rejected():
    body = "Karnataka is a state in south western India, and its capital is Bengaluru, the largest city in the state."
    gate = QualityAgent()

    first = gate.check(make_document(url="https://a.gov.in/", body_text=body))
    second = gate.check(make_document(url="https://a.gov.in/", body_text=body))

    assert first.ok is True
    assert second.ok is False
    assert second.reason == "duplicate"


def test_the_same_body_at_another_url_is_a_different_page():
    """Two sites sharing a template preamble are two pages, not a duplicate."""
    body = "Karnataka is a state in south western India, and its capital is Bengaluru, the largest city in the state."
    gate = QualityAgent()

    first = gate.check(make_document(url="https://a.gov.in/", body_text=body))
    second = gate.check(make_document(url="https://b.gov.in/", body_text=body))

    assert first.ok is True
    assert second.ok is True


def test_reset_clears_the_seen_digests():
    body = "Karnataka is a state in south western India, and its capital is Bengaluru, the largest city in the state."
    gate = QualityAgent()

    gate.check(make_document(body_text=body))
    gate.reset()

    assert gate.check(make_document(body_text=body)).ok is True


def test_a_waf_interstitial_is_rejected_as_junk():
    body = (
        "403 Forbidden Access denied. This request was blocked by the security rules "
        "in force for this site, and will not be served to this client."
    )

    verdict = QualityAgent().check(make_document(body_text=body))

    assert verdict.ok is False
    assert verdict.reason == "junk_page"


def test_a_page_without_a_title_is_rejected():
    verdict = QualityAgent().check(make_document(title=""))

    assert verdict.ok is False
    assert verdict.reason == "no_title"


def test_a_good_page_passes_with_a_positive_score():
    verdict = QualityAgent().check(make_document())

    assert verdict.ok is True
    assert 0.0 < verdict.score <= 1.0
    assert len(verdict.digest) == 64


def test_digest_of_ignores_surrounding_whitespace():
    assert digest_of("  karnataka  ") == digest_of("karnataka")
