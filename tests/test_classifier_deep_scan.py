"""Deep-scan classification: the four signals, in order of authority."""

from __future__ import annotations

import pytest

from app.agents import load_agent

ClassifierAgent = load_agent("07_classifier_agent").ClassifierAgent


@pytest.fixture()
def classifier():
    return ClassifierAgent()


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.paytm.com/recharge", "business_finance"),
        ("https://indiankanoon.org/doc/1", "legal_judiciary"),
        ("https://www.thehindu.com/news/x", "news_media"),
        ("https://iitb.ac.in/admissions", "education_exams"),
        ("https://krishijagran.com/crops", "food_dining"),
        ("https://www.practo.com/", "health_medical"),
        ("https://zerodha.com/", "business_finance"),
        ("https://github.com/x/y", "tech_developer"),
        ("https://amazon.in/x", "commerce"),
        ("https://naukri.com/x", "jobs_careers"),
        ("https://karnataka.gov.in/x", "government"),
    ],
)
def test_a_named_domain_decides(classifier, url, expected):
    """Signal 1: the curated list answers without scoring anything."""
    assert classifier.classify(url, "Portal", "", "").category == expected


def test_a_specialised_domain_beats_the_government_suffix(classifier):
    """sci.gov.in is a court, not a ministry; icar.org.in is agriculture."""
    assert classifier.classify("https://sci.gov.in/x", "", "", "").category == "legal_judiciary"
    assert classifier.classify("https://icar.org.in/x", "", "", "").category == "food_dining"


def test_a_named_site_beats_a_bare_suffix(classifier):
    """aiims.edu is a hospital that happens to sit under .edu."""
    assert classifier.classify("https://aiims.edu/hospital", "Hospital", "", "").category == "health_medical"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://highcourt.gov.in/judgment", "legal_judiciary"),
        ("https://ssc.nic.in/recruitment", "jobs_careers"),
        ("https://krishi.gov.in/mandi", "food_dining"),
    ],
)
def test_the_path_refines_a_government_suffix(classifier, url, expected):
    """Signal 2: a suffix says 'government', the path says which department."""
    assert classifier.classify(url, "", "", "").category == expected


def test_the_title_decides_when_the_url_says_nothing(classifier):
    """Signal 3: phrases a page uses about itself."""
    result = classifier.classify("https://example.com/x", "Kharif and rabi crops", "", "")

    assert result.category == "food_dining"


def test_a_page_with_no_signal_falls_back_to_general_reference(classifier):
    """Signal 4: WEB is the absence of a decision, not a decision."""
    assert classifier.classify("https://randomshop.com/", "Home", "", "").category == "general_reference"


# ---------------------------------------------------------------------------
# Path intent, mapped onto the twelve codes this system has
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://x.gov.in/careers", "jobs_careers"),
        ("https://x.gov.in/court-orders", "legal_judiciary"),
        ("https://x.in/netbanking", "business_finance"),
        ("https://shop.in/cart", "commerce"),
        ("https://x.gov.in/fir-status", "government"),
        ("https://x.ac.in/admissions", "education_exams"),
    ],
)
def test_a_path_says_what_the_page_is_for(classifier, url, expected):
    assert classifier.classify(url, "", "", "").category == expected


def test_the_court_check_keeps_courts_out_of_government(classifier):
    """.gov.in is government, except where the host says it is a court."""
    assert classifier.classify("https://ecourts.gov.in/x", "", "", "").category == "legal_judiciary"
