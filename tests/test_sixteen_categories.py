"""The sixteen-category taxonomy and the cases the specification names."""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.schema_postgres import CATEGORY_SEED

module = load_agent("07_classifier_agent")
ALLOWED_CATEGORIES = module.ALLOWED_CATEGORIES


@pytest.fixture()
def classifier():
    return module.ClassifierAgent()


def test_there_are_exactly_sixteen_codes():
    assert ALLOWED_CATEGORIES == (
        "GOV", "FIN", "LEGAL", "TECH", "EDU", "NEWS", "JOBS", "HEALTH",
        "TRANS", "UTIL", "COMM", "SOCIAL", "CORP", "FOOD", "MEDIA", "WEB",
    )


def test_the_schema_seeds_the_same_sixteen():
    """The seed and the classifier cannot drift apart."""
    assert tuple(code for code, _ in CATEGORY_SEED) == ALLOWED_CATEGORIES


# ---------------------------------------------------------------------------
# The verification cases from the specification
# ---------------------------------------------------------------------------


def test_a_court_is_legal_not_government(classifier):
    assert classifier.classify("https://ecourts.gov.in/services", "", "", "").category == "legal_judiciary"


def test_a_path_override_beats_the_domain(classifier):
    """sbi.co.in is a bank; its careers page is a job listing."""
    assert classifier.classify("https://ksp.karnataka.gov.in/careers", "", "", "").category == "jobs_careers"


def test_a_bank_login_is_finance(classifier):
    assert classifier.classify("https://sbi.co.in/portal/login", "", "", "").category == "business_finance"


def test_a_utility_payment_is_utilities(classifier):
    assert classifier.classify("https://bescom.karnataka.gov.in/quick-pay", "", "", "").category == "utilities"


def test_a_legal_sentence_classifies_without_a_domain_hint(classifier):
    result = classifier.classify(
        "https://unknown.example/", "Bail application dismissed by division bench", "", ""
    )

    assert result.category == "legal_judiciary"


def test_a_page_with_no_signal_is_quarantined(classifier):
    """WEB is the quarantine, not a default."""
    assert classifier.classify("https://ablog.example/", "My thoughts today", "", "").category == "general_reference"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://irctc.co.in/x", "travel_transit"),
        ("https://ksrtc.in/x", "travel_transit"),
        ("https://bescom.karnataka.gov.in/x", "utilities"),
        ("https://reddit.com/r/x", "community_social"),
        ("https://zomato.com/x", "food_dining"),
        ("https://youtube.com/watch", "entertainment_media"),
    ],
)
def test_each_new_category_is_reachable(classifier, url, expected):
    assert classifier.classify(url, "", "", "").category == expected
