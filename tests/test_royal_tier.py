"""Agents 22 to 30: loadability, trust, spelling, schema, ranking, reporting."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from app.agents import load_agent

ROYAL = (
    "22_queen_policy_agent",
    "23_astrologer_trend_agent",
    "24_sabotage_shield_agent",
    "25_librarian_schema_agent",
    "26_semantic_sage_agent",
    "27_rank_general_agent",
    "28_visual_herald_agent",
    "29_memory_janitor_agent",
    "30_chronicler_reporter_agent",
)


@pytest.mark.parametrize("name", ROYAL)
def test_every_royal_agent_loads(name):
    assert load_agent(name) is not None


# ---------------------------------------------------------------------------
# 22 — trust
# ---------------------------------------------------------------------------


def test_an_ordinary_page_is_accepted():
    queen = load_agent("22_queen_policy_agent").QueenPolicyAgent()
    verdict = queen.judge("https://karnataka.gov.in/x", "Health", "district health notifications")

    assert verdict.accepted is True
    assert verdict.score == 1.0


def test_a_phishing_page_is_rejected_with_a_reason():
    queen = load_agent("22_queen_policy_agent").QueenPolicyAgent()
    verdict = queen.judge(
        "https://paypal-secure-login.example/x",
        "",
        "verify your account and enter your card number, urgent action required",
    )

    assert verdict.accepted is False
    assert any(reason.startswith("phishing_language") for reason in verdict.reasons)
    assert any(reason.startswith("impersonates") for reason in verdict.reasons)


def test_a_brand_on_its_own_domain_is_not_impersonation():
    impersonation = load_agent("22_queen_policy_agent").brand_impersonation

    assert impersonation("https://paypal.com/x") == ""
    assert impersonation("https://www.paypal.com/x") == ""
    assert impersonation("https://paypal-secure.example/x") == "paypal"


# ---------------------------------------------------------------------------
# 23 — freshness ordering
# ---------------------------------------------------------------------------


def test_the_newest_feed_entry_is_crawled_first():
    rank = load_agent("23_astrologer_trend_agent").rank_fresh
    feed = (
        "<rss><channel>"
        "<item><link>https://a.com/old</link>"
        "<pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>"
        "<item><link>https://a.com/new</link>"
        "<pubDate>Wed, 29 Oct 2026 10:00:00 +0000</pubDate></item>"
        "</channel></rss>"
    )

    assert rank(feed)[0] == "https://a.com/new"


def test_a_feed_without_dates_keeps_its_order():
    rank = load_agent("23_astrologer_trend_agent").rank_fresh
    feed = "<rss><channel><item><link>https://a.com/1</link></item><item><link>https://a.com/2</link></item></channel></rss>"

    assert rank(feed) == ["https://a.com/1", "https://a.com/2"]


# ---------------------------------------------------------------------------
# 24 — the shield blocks repeat offenders
# ---------------------------------------------------------------------------


def test_a_client_that_keeps_flooding_is_blocked():
    module = load_agent("24_sabotage_shield_agent")
    now = [0.0]
    limiter = module.TokenBucketLimiter(capacity=2, window_seconds=60.0, clock=lambda: now[0])
    shield = module.SabotageShieldAgent(limiter, strikes=2, block_seconds=900, clock=lambda: now[0])

    # Spend the bucket.
    assert shield.check("1.2.3.4").allowed is True
    assert shield.check("1.2.3.4").allowed is True

    # First refusal is a throttle, not a block.
    assert shield.check("1.2.3.4").allowed is False
    assert shield.blocked("1.2.3.4") == 0.0

    # Second refusal blocks for the full window.
    assert shield.check("1.2.3.4").allowed is False
    assert shield.blocked("1.2.3.4") == 900.0


def test_a_block_expires():
    module = load_agent("24_sabotage_shield_agent")
    now = [0.0]
    limiter = module.TokenBucketLimiter(capacity=1, window_seconds=60.0, clock=lambda: now[0])
    shield = module.SabotageShieldAgent(limiter, strikes=1, block_seconds=100, clock=lambda: now[0])

    shield.check("x")  # spends the token
    shield.check("x")  # blocks
    assert shield.blocked("x") == 100.0

    now[0] = 101.0
    assert shield.blocked("x") == 0.0


# ---------------------------------------------------------------------------
# 25 — schema extraction
# ---------------------------------------------------------------------------


def test_the_librarian_reads_json_ld_and_open_graph():
    librarian = load_agent("25_librarian_schema_agent").LibrarianSchemaAgent()
    html = """
    <html><head>
      <meta property="og:title" content="District Health Notifications">
      <script type="application/ld+json">
        {"@type": "NewsArticle", "headline": "Health notices"}
      </script>
    </head><body><table><tr><td>1</td></tr></table></body></html>
    """
    found = librarian.extract(BeautifulSoup(html, "html.parser"), "https://a.com/x")

    assert "newsarticle" in found["schema_types"]
    assert found["kinds"] == ["article"]
    assert found["open_graph"]["og:title"] == "District Health Notifications"
    assert found["tables"] == 1


# ---------------------------------------------------------------------------
# 26 — spelling
# ---------------------------------------------------------------------------


def test_a_misspelling_is_corrected_against_the_vocabulary():
    sage = load_agent("26_semantic_sage_agent").SemanticSageAgent()
    sage.learn("health notifications district office karnataka government passport")

    assert sage.suggest("helth").corrected == "health"
    assert sage.suggest("notificatons").corrected == "notifications"
    assert sage.suggest("pasport").corrected == "passport"


def test_a_word_it_cannot_place_is_left_alone():
    sage = load_agent("26_semantic_sage_agent").SemanticSageAgent()
    sage.learn("health notifications")

    suggestion = sage.suggest("zzzz")
    assert suggestion.corrected == "zzzz"
    assert suggestion.changed is False


def test_a_correct_query_is_not_rewritten():
    sage = load_agent("26_semantic_sage_agent").SemanticSageAgent()
    sage.learn("health notifications")

    assert sage.suggest("health notifications").changed is False


# ---------------------------------------------------------------------------
# 27 — ranking
# ---------------------------------------------------------------------------


def test_the_composite_score_combines_its_parts():
    module = load_agent("27_rank_general_agent")

    plain = module.composite_score(lexical=0.5)
    titled = module.composite_score(lexical=0.5, title_hit=True)
    full = module.composite_score(lexical=0.5, title_hit=True, authority_hit=True, quality=1.0)

    assert plain < titled < full


def test_a_title_hit_outranks_a_barely_lexical_match():
    module = load_agent("27_rank_general_agent")

    assert module.composite_score(lexical=0.1, title_hit=True) > module.composite_score(lexical=0.9)


# ---------------------------------------------------------------------------
# 28 — favicons
# ---------------------------------------------------------------------------


def test_the_declared_icon_beats_the_guessed_one():
    herald = load_agent("28_visual_herald_agent")
    html = '<html><head><link rel="icon" href="/assets/icon-32.png"></head></html>'
    soup = BeautifulSoup(html, "html.parser")

    assert herald.icon_from_html(soup, "https://a.com/page") == "https://a.com/assets/icon-32.png"


def test_a_page_with_no_icon_falls_back_to_the_root():
    herald = load_agent("28_visual_herald_agent")
    soup = BeautifulSoup("<html></html>", "html.parser")

    assert herald.icon_from_html(soup, "https://a.com/x") == ""
    assert herald.fallback_icon("https://a.com/x") == "https://a.com/favicon.ico"


def test_a_cached_path_is_only_reported_when_the_file_exists(tmp_path):
    herald = load_agent("28_visual_herald_agent").VisualHeraldAgent(cache_dir=str(tmp_path))

    assert herald.cached_path("https://a.com/x") == ""
    (tmp_path / "a.com.icon").write_bytes(b"x")
    assert herald.cached_path("https://a.com/x").endswith("a.com.icon")


# ---------------------------------------------------------------------------
# 29 — the janitor
# ---------------------------------------------------------------------------


def test_the_janitor_does_not_collect_on_a_small_process():
    janitor = load_agent("29_memory_janitor_agent").MemoryJanitorAgent(threshold_bytes=1 << 40)

    assert janitor.should_collect(100 * 1024 * 1024) is False
    assert janitor.should_collect(1 << 41) is True


@pytest.mark.asyncio
async def test_a_sweep_below_the_threshold_skips_the_collection():
    janitor = load_agent("29_memory_janitor_agent").MemoryJanitorAgent(threshold_bytes=1 << 40)

    report = await janitor.sweep()

    assert report.collected is False
    assert janitor.collections == 0


# ---------------------------------------------------------------------------
# 30 — the chronicler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_chronicler_reports_what_the_database_holds(client, settings):
    from app.engine.store import IndexStore, PageDocument

    store = IndexStore(client, settings)
    await store.persist_batch(
        [
            PageDocument(
                url="https://a.gov.in/1",
                domain="a.gov.in",
                title="Health",
                description="d",
                body_text="district health",
                digest="r1",
                category="government",
            )
        ]
    )

    report = await load_agent("30_chronicler_reporter_agent").ChroniclerReporterAgent(
        client, settings
    ).build()

    assert report.indexed_pages == 1
    assert report.categories.get("GOV") == 1
    assert "Eyesearch daily report" in report.as_markdown()
    assert report.crawler_enabled is False
