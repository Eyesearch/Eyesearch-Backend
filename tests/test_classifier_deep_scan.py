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
        ("https://www.paytm.com/recharge", "payments"),
        ("https://indiankanoon.org/doc/1", "legal_judiciary"),
        ("https://www.thehindu.com/news/x", "news_media"),
        ("https://iitb.ac.in/admissions", "education_exams"),
        ("https://krishijagran.com/crops", "agriculture_rural"),
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
    assert classifier.classify("https://icar.org.in/x", "", "", "").category == "agriculture_rural"


def test_a_named_site_beats_a_bare_suffix(classifier):
    """aiims.edu is a hospital that happens to sit under .edu."""
    assert classifier.classify("https://aiims.edu/hospital", "Hospital", "", "").category == "health_medical"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://highcourt.gov.in/judgment", "legal_judiciary"),
        ("https://ssc.nic.in/recruitment", "jobs_careers"),
        ("https://krishi.gov.in/mandi", "agriculture_rural"),
    ],
)
def test_the_path_refines_a_government_suffix(classifier, url, expected):
    """Signal 2: a suffix says 'government', the path says which department."""
    assert classifier.classify(url, "", "", "").category == expected


def test_the_title_decides_when_the_url_says_nothing(classifier):
    """Signal 3: phrases a page uses about itself."""
    result = classifier.classify("https://example.com/x", "Kharif and rabi crops", "", "")

    assert result.category == "agriculture_rural"


def test_a_page_with_no_signal_falls_back_to_general_reference(classifier):
    """Signal 4: WEB is the absence of a decision, not a decision."""
    assert classifier.classify("https://randomshop.com/", "Home", "", "").category == "general_reference"
