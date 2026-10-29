"""Agents 31 to 40: Kannada stemming, the trie, breadcrumbs, PDF headers and the
rest of the specialist tier."""

from __future__ import annotations

import zlib

import pytest

from app.agents import load_agent

SPECIALISTS = (
    "31_indic_kannada_agent",
    "32_media_pdf_agent",
    "33_broken_link_reaper_agent",
    "34_breadcrumb_builder_agent",
    "35_cold_start_booster_agent",
    "36_copyright_legal_agent",
    "37_relevance_feedback_agent",
    "38_disaster_snapshot_agent",
    "39_autosuggest_trie_agent",
    "40_workload_balancer_agent",
)


@pytest.mark.parametrize("name", SPECIALISTS)
def test_every_specialist_agent_loads(name):
    assert load_agent(name) is not None


# ---------------------------------------------------------------------------
# 31 — Kannada morphology
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "word, stem",
    [
        ("ಬೆಂಗಳೂರಿನಲ್ಲಿ", "ಬೆಂಗಳೂರು"),   # in Bangalore
        ("ಬೆಂಗಳೂರಿಗೆ", "ಬೆಂಗಳೂರು"),       # to Bangalore
        ("ಬೆಂಗಳೂರು", "ಬೆಂಗಳೂರು"),          # Bangalore
        ("ಕರ್ನಾಟಕದ", "ಕರ್ನಾಟಕ"),            # of Karnataka
        ("ಸರ್ಕಾರದಿಂದ", "ಸರ್ಕಾರ"),           # by the government
    ],
)
def test_kannada_suffixes_are_stripped(word, stem):
    assert load_agent("31_indic_kannada_agent").strip_suffix(word) == stem


def test_a_short_word_is_not_reduced_to_a_fragment():
    strip = load_agent("31_indic_kannada_agent").strip_suffix
    # Stripping would leave two characters, which matches everything.
    assert strip("ಮಗನ") == "ಮಗನ"


def test_english_is_left_alone():
    assert load_agent("31_indic_kannada_agent").strip_suffix("bangalore") == "bangalore"


def test_expansion_offers_both_forms():
    expand = load_agent("31_indic_kannada_agent").IndicKannadaAgent().expand("ಬೆಂಗಳೂರಿನಲ್ಲಿ")

    assert "ಬೆಂಗಳೂರಿನಲ್ಲಿ" in expand
    assert "ಬೆಂಗಳೂರು" in expand


# ---------------------------------------------------------------------------
# 32 — PDF header sniffing
# ---------------------------------------------------------------------------


def _pdf_with_text(text: str) -> bytes:
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    stream = zlib.compress(content)
    header = b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\nstream\n"
    return header + stream + b"\nendstream\n%%EOF"


def test_a_pdf_snippet_is_read_from_the_first_stream():
    agent = load_agent("32_media_pdf_agent").MediaPdfAgent()
    data = _pdf_with_text("Karnataka Gazette Notification 2026")

    assert agent.looks_like_pdf(data) is True
    assert agent.snippet(data) == "Karnataka Gazette Notification 2026"[:40]


def test_the_snippet_never_exceeds_the_ceiling():
    agent = load_agent("32_media_pdf_agent").MediaPdfAgent()
    data = _pdf_with_text("x" * 400)

    assert len(agent.snippet(data)) <= 40


def test_something_that_is_not_a_pdf_yields_nothing():
    agent = load_agent("32_media_pdf_agent").MediaPdfAgent()

    assert agent.looks_like_pdf(b"<html>no</html>") is False
    assert agent.snippet(b"<html>no</html>") == ""


# ---------------------------------------------------------------------------
# 33 — the reaper counts before it deletes
# ---------------------------------------------------------------------------


def test_one_404_is_not_enough_to_delete_a_page():
    reaper = load_agent("33_broken_link_reaper_agent").BrokenLinkReaperAgent(None, strikes=3)

    reaper.record("https://a.com/x", 404)
    reaper.record("https://a.com/x", 404)
    assert reaper.should_purge("https://a.com/x") is False

    reaper.record("https://a.com/x", 404)
    assert reaper.should_purge("https://a.com/x") is True


def test_a_server_error_never_counts_as_gone():
    module = load_agent("33_broken_link_reaper_agent")
    reaper = module.BrokenLinkReaperAgent(None, strikes=1)

    for _ in range(5):
        reaper.record("https://a.com/x", 503)

    assert reaper.should_purge("https://a.com/x") is False
    assert module.classify_status(503) == "transient"
    assert module.classify_status(200) == "alive"
    assert module.classify_status(410) == "gone"


# ---------------------------------------------------------------------------
# 34 — breadcrumbs
# ---------------------------------------------------------------------------


def test_a_path_becomes_readable_segments():
    builder = load_agent("34_breadcrumb_builder_agent").BreadcrumbBuilderAgent()
    crumb = builder.build("https://karnataka.gov.in/dept/fin/2026/tax/index.html")

    # The year is navigation, not a subject, and index.html is noise.
    assert crumb.as_list() == ["Dept", "Fin", "Tax"]
    assert crumb.as_string() == "karnataka.gov.in › Dept › Fin › Tax"


def test_a_root_url_has_no_segments():
    builder = load_agent("34_breadcrumb_builder_agent").BreadcrumbBuilderAgent()

    assert builder.build("https://a.com/").as_list() == []


# ---------------------------------------------------------------------------
# 35 — seed weight
# ---------------------------------------------------------------------------


def test_authoritative_seeds_are_crawled_first():
    booster = load_agent("35_cold_start_booster_agent").ColdStartBoosterAgent()

    ordered = booster.order(
        ["https://shop.example/a", "https://karnataka.gov.in/b", "https://iitb.ac.in/c"]
    )

    assert ordered[0] == "https://karnataka.gov.in/b"
    assert ordered[-1] == "https://shop.example/a"


# ---------------------------------------------------------------------------
# 36 — piracy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/torrent/movie",
        "https://x.com/warez/app",
        "magnet:?xt=urn:btih:abc",
        "https://x.com/file.torrent",
    ],
)
def test_piracy_is_refused(url):
    assert load_agent("36_copyright_legal_agent").piracy_signal(url) != ""


def test_an_ordinary_page_is_allowed():
    assert load_agent("36_copyright_legal_agent").piracy_signal("https://karnataka.gov.in/x") == ""


# ---------------------------------------------------------------------------
# 37 — feedback
# ---------------------------------------------------------------------------


def test_a_query_that_is_never_clicked_is_penalised():
    agent = load_agent("37_relevance_feedback_agent").RelevanceFeedbackAgent()
    for _ in range(20):
        agent.record_shown("bad query")

    assert agent.adjustment("bad query") < 1.0


def test_a_query_clicked_near_the_top_is_left_alone():
    agent = load_agent("37_relevance_feedback_agent").RelevanceFeedbackAgent()
    for _ in range(20):
        agent.record_shown("good query")
    agent.record_click("good query", 1)

    assert agent.adjustment("good query") == 1.0


def test_an_unknown_query_is_neutral():
    assert load_agent("37_relevance_feedback_agent").RelevanceFeedbackAgent().adjustment("x") == 1.0


# ---------------------------------------------------------------------------
# 38 — snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_snapshot_round_trips(client, settings, tmp_path):
    from app.engine.store import IndexStore, PageDocument

    store = IndexStore(client, settings)
    await store.persist_batch(
        [
            PageDocument(
                url="https://a.gov.in/1",
                domain="a.gov.in",
                title="Health",
                description="d",
                body_text="text",
                digest="s1",
                category="government",
            )
        ]
    )

    agent = load_agent("38_disaster_snapshot_agent").DisasterSnapshotAgent(client, settings)
    path = str(tmp_path / "snap.json.gz")
    report = await agent.write(path)

    assert report.domains >= 1
    assert report.categories >= 12
    restored = agent.read(path)
    assert "a.gov.in" in restored["domains"]
    assert restored["categories"]["GOV"] == "government"


# ---------------------------------------------------------------------------
# 39 — the trie
# ---------------------------------------------------------------------------


def test_prefix_completion_is_ordered_by_frequency():
    agent = load_agent("39_autosuggest_trie_agent").TrieAutosuggestAgent()
    agent.rebuild([("bangalore", 50), ("bank", 40), ("bandipur", 10)])

    assert agent.suggest("ban") == ["bangalore", "bank", "bandipur"]


def test_a_short_prefix_returns_nothing():
    agent = load_agent("39_autosuggest_trie_agent").TrieAutosuggestAgent()
    agent.rebuild([("bangalore", 50)])

    assert agent.suggest("b") == []


def test_a_prefix_that_matches_nothing_returns_nothing():
    agent = load_agent("39_autosuggest_trie_agent").TrieAutosuggestAgent()
    agent.rebuild([("bangalore", 50)])

    assert agent.suggest("zz") == []


# ---------------------------------------------------------------------------
# 40 — the balancer
# ---------------------------------------------------------------------------


def test_concurrency_falls_to_the_floor_when_searches_are_slow():
    agent = load_agent("40_workload_balancer_agent").WorkloadBalancerAgent(max_concurrency=60)
    for latency in (400, 500, 600):
        agent.observe(latency)

    state = agent.state()
    assert state.mode == "busy"
    assert state.concurrency == 2


def test_concurrency_is_full_when_searches_are_fast():
    agent = load_agent("40_workload_balancer_agent").WorkloadBalancerAgent(max_concurrency=60)
    for latency in (50, 60, 80):
        agent.observe(latency)

    assert agent.state().concurrency == 60


def test_the_floor_is_never_zero():
    agent = load_agent("40_workload_balancer_agent").WorkloadBalancerAgent(max_concurrency=60)
    for _ in range(10):
        agent.observe(9999)

    assert agent.concurrency() >= 1
