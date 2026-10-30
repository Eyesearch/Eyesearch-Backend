"""Classifier and language agents, and the filters they enable.

Both run at ingestion so a search can be narrowed later without a backfill.
The tests cover the agents themselves, the persistence of their output, and the
search filters that consume it.
"""

from __future__ import annotations

import pytest

from app.agents import load_agent
from app.engine.store import PageDocument
from app.services.search_service import SearchService

classifier_module = load_agent("07_classifier_agent")
language_module = load_agent("08_language_agent")

CATEGORIES = classifier_module.CATEGORIES
DEFAULT_CATEGORY = classifier_module.DEFAULT_CATEGORY
ClassifierAgent = classifier_module.ClassifierAgent
DEFAULT_LANGUAGE = language_module.DEFAULT_LANGUAGE
LanguageAgent = language_module.LanguageAgent
SUPPORTED_LANGUAGES = language_module.SUPPORTED_LANGUAGES

KANNADA = "ಕರ್ನಾಟಕ ಸರ್ಕಾರವು ನಾಗರಿಕ ಸೇವೆಗಳನ್ನು ಒದಗಿಸುತ್ತದೆ."
DEVANAGARI = "कर्नाटक सरकार नागरिक सेवाएं प्रदान करती है।"

BODY = (
    "The department publishes notifications, citizen services and contact details "
    "for district offices across the state."
)


def _page(url: str, title: str, body: str, category: str, language: str = "en") -> PageDocument:
    return PageDocument(
        url=url,
        domain=url.split("//", 1)[1].split("/", 1)[0].replace("www.", ""),
        title=title,
        description=f"{title} description.",
        body_text=body,
        digest=f"{url}-digest",
        category=category,
        language=language,
    )


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def test_the_sixteen_categories_are_exactly_the_specified_ones():
    """The taxonomy is data, so it is asserted as data."""
    assert CATEGORIES == (
        "government",
        "business_finance",
        "legal_judiciary",
        "tech_developer",
        "education_exams",
        "news_media",
        "jobs_careers",
        "health_medical",
        "travel_transit",
        "utilities",
        "commerce",
        "community_social",
        "business_corp",
        "food_dining",
        "entertainment_media",
        "general_reference",
    )


def test_the_published_codes_are_sixteen():
    from app.agents import load_agent

    assert len(load_agent("07_classifier_agent").ALLOWED_CATEGORIES) == 16


@pytest.mark.parametrize(
    ("url", "title", "body", "expected"),
    [
        (
            "https://karnataka.gov.in/schemes",
            "Citizen services",
            "Government of Karnataka citizen services portal.",
            "government",
        ),
        (
            "https://iisc.ac.in/admissions/syllabus",
            "Admission syllabus",
            "The examination syllabus for the academic year is published.",
            "education_exams",
        ),
        (
            "https://example.in/news/breaking",
            "Breaking news",
            "Our correspondent reported that the press release followed the editorial.",
            "news_media",
        ),
        (
            "https://ssc.nic.in/recruitment",
            "Recruitment notification",
            "Applications are invited; check the eligibility criteria and last date.",
            "jobs_careers",
        ),
        (
            "https://highcourt.gov.in/judgment",
            "Judgment",
            "The supreme court judgment was delivered on the writ petition.",
            "legal_judiciary",
        ),
        (
            "https://rbi.org.in/gst/msme",
            "GST and MSME",
            "The repo rate and gst collections affect msme lending.",
            "business_finance",
        ),
        (
            "https://github.com/api/docs",
            "API reference",
            "The open source repository documentation covers the api reference.",
            "tech_developer",
        ),
        (
            "https://aiims.edu/hospital",
            "Hospital services",
            "The hospital outpatient department and vaccination counters are open.",
            "health_medical",
        ),
        (
            "https://krishi.gov.in/mandi",
            "Mandi prices",
            "Farmers can check the minimum support price at the mandi.",
            "food_dining",
        ),
    ],
)
def test_each_category_is_reachable(url, title, body, expected):
    verdict = ClassifierAgent().classify(url, title=title, body_text=body)

    assert verdict.category == expected
    assert verdict.matched


def test_a_gov_in_domain_is_government_even_with_generic_copy():
    verdict = ClassifierAgent().classify("https://somewhere.gov.in/", title="Home")

    assert verdict.category == "government"


def test_an_unrelated_page_falls_back_to_general_reference():
    verdict = ClassifierAgent().classify(
        "https://example.com/about",
        title="About us",
        body_text="A short note about the organisation and the people who work here.",
    )

    assert verdict.category == DEFAULT_CATEGORY
    assert verdict.is_fallback is True


def test_classification_is_deterministic():
    first = ClassifierAgent().classify("https://rbi.org.in/gst", title="GST", body_text="gst")
    second = ClassifierAgent().classify("https://rbi.org.in/gst", title="GST", body_text="gst")

    assert first.category == second.category
    assert first.score == second.score


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (KANNADA, "kn"),
        (DEVANAGARI, "hi"),
        ("Karnataka government citizen services portal.", "en"),
        ("", "en"),
        ("   ", "en"),
        ("12345", "en"),
    ],
)
def test_language_detection(text, expected):
    assert LanguageAgent().detect(text) == expected


def test_a_little_kannada_in_english_text_does_not_change_the_language():
    text = "Karnataka government services portal. ಕನ್ನಡ"

    assert LanguageAgent().detect(text) == "en"


def test_the_dominant_script_wins_when_both_are_present():
    mixed = KANNADA * 3 + DEVANAGARI

    assert LanguageAgent().detect(mixed) == "kn"


def test_detection_reports_its_evidence():
    detail = LanguageAgent().detect_detail(KANNADA)

    assert detail.language == "kn"
    assert detail.counts["kn"] > 0
    assert detail.share > 0


def test_supported_languages_cover_the_major_indian_scripts():
    assert SUPPORTED_LANGUAGES == (
        "en", "hi", "kn", "ta", "te", "ml", "bn", "gu", "pa", "or", "ar",
    )
    assert DEFAULT_LANGUAGE == "en"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("தமிழ் மொழி", "ta"),
        ("తెలుగు భాష", "te"),
        ("മലയാളം ഭാഷ", "ml"),
        ("বাংলা ভাষা", "bn"),
        ("ગુજરાતી ભાષા", "gu"),
        ("ਪੰਜਾਬੀ ਭਾਸ਼ਾ", "pa"),
        ("ଓଡ଼ିଆ ଭାଷା", "or"),
        ("اردو زبان", "ar"),
    ],
)
def test_each_indian_script_is_detected(text, expected):
    assert LanguageAgent().detect(text) == expected


def test_a_short_bengali_word_is_not_mistaken_for_english():
    """Combining marks are not `isalpha()`, so they must still count."""
    assert LanguageAgent().detect("বাংলা") == "bn"


# ---------------------------------------------------------------------------
# Persistence and filters
# ---------------------------------------------------------------------------


async def test_a_page_keeps_its_category_and_language(store):
    await store.persist_batch(
        [_page("https://karnataka.gov.in/", "Karnataka", BODY, "government", "kn")]
    )

    stored = await store.get_page("https://karnataka.gov.in/")

    assert stored["category"] == "government"
    assert stored["language"] == "kn"


async def test_a_page_defaults_when_it_is_not_tagged(store):
    document = PageDocument(
        url="https://example.in/x",
        domain="example.in",
        title="Something",
        description="d",
        body_text=BODY,
        digest="untagged",
    )

    await store.persist_batch([document])
    stored = await store.get_page("https://example.in/x")

    assert stored["category"] == DEFAULT_CATEGORY
    assert stored["language"] == DEFAULT_LANGUAGE


async def test_search_can_be_filtered_by_category(store):
    await store.persist_batch(
        [
            _page("https://a.gov.in/metro", "Metro timings", BODY + " metro", "government"),
            _page("https://b.gov.in/metro", "Metro fares", BODY + " metro", "business_finance"),
        ]
    )

    hits = await store.search("metro", category="government")

    assert [hit.url for hit in hits] == ["https://a.gov.in/metro"]


async def test_search_can_be_filtered_by_language(store):
    await store.persist_batch(
        [
            _page("https://a.gov.in/", "Metro timings", BODY + " metro", "government", "en"),
            _page("https://b.gov.in/", "Metro timings", BODY + " metro", "government", "kn"),
        ]
    )

    hits = await store.search("metro", language="kn")

    assert [hit.url for hit in hits] == ["https://b.gov.in/"]


async def test_both_filters_apply_together(store):
    await store.persist_batch(
        [
            _page("https://a.gov.in/", "Metro", BODY + " metro", "government", "kn"),
            _page("https://b.gov.in/", "Metro", BODY + " metro", "government", "en"),
            _page("https://c.gov.in/", "Metro", BODY + " metro", "news_media", "kn"),
        ]
    )

    hits = await store.search("metro", category="government", language="kn")

    assert [hit.url for hit in hits] == ["https://a.gov.in/"]


async def test_the_service_reports_the_applied_filters(store):
    service = SearchService(store)

    payload = await service.search("metro", category="government", language="kn")

    assert payload["filters"] == {"category": "government", "language": "kn"}


def test_an_unknown_category_is_rejected():
    with pytest.raises(ValueError):
        SearchService.normalise_category("astrology")


def test_an_unknown_language_is_rejected():
    with pytest.raises(ValueError):
        SearchService.normalise_language("fr")


def test_no_filter_is_a_valid_filter():
    assert SearchService.normalise_category(None) is None
    assert SearchService.normalise_category("") is None
    assert SearchService.normalise_language(None) is None


def test_filters_are_case_insensitive():
    assert SearchService.normalise_category("GOVERNMENT") == "government"
    assert SearchService.normalise_language("KN") == "kn"


async def test_revalidation_does_not_wipe_the_classification(store):
    """update_page refreshes content; it must not reset the tags."""
    await store.persist_batch(
        [_page("https://karnataka.gov.in/", "Karnataka", BODY, "government", "kn")]
    )

    refreshed = PageDocument(
        url="https://karnataka.gov.in/",
        domain="karnataka.gov.in",
        title="Karnataka, refreshed",
        description="Refreshed description.",
        body_text=BODY + " Updated.",
        digest="refreshed-digest",
    )
    await store.update_page(refreshed)

    stored = await store.get_page("https://karnataka.gov.in/")
    assert stored["title"] == "Karnataka, refreshed"
    assert stored["category"] == "government"
    assert stored["language"] == "kn"
