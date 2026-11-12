"""Per-domain result clustering and vocabulary suggestions."""

from __future__ import annotations

import pytest

from app.engine.config import Settings
from app.engine.store import IndexStore, PageDocument
from app.agents import load_agent

cluster_by_domain = load_agent("13_dedupe_compact_agent").cluster_by_domain

BODY = "karnataka health district notification office government scheme passport " * 2


class Hit:
    def __init__(self, url: str) -> None:
        self.url = url


def urls(hits) -> list[str]:
    return [hit.url for hit in hits]


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def test_one_domain_cannot_take_the_whole_page():
    hits = [Hit(f"https://a.com/{i}") for i in range(8)]

    assert urls(cluster_by_domain(hits, per_domain=2, limit=10)) == [
        "https://a.com/0",
        "https://a.com/1",
    ]


def test_other_domains_get_through():
    hits = [Hit(f"https://a.com/{i}") for i in range(5)] + [Hit("https://b.com/1")]

    assert urls(cluster_by_domain(hits, per_domain=2, limit=3)) == [
        "https://a.com/0",
        "https://a.com/1",
        "https://b.com/1",
    ]


def test_rank_order_is_preserved():
    hits = [Hit("https://a.com/1"), Hit("https://b.com/1"), Hit("https://a.com/2")]

    assert urls(cluster_by_domain(hits, per_domain=1, limit=10)) == [
        "https://a.com/1",
        "https://b.com/1",
    ]


def test_a_cap_of_zero_means_no_cap():
    hits = [Hit(f"https://a.com/{i}") for i in range(4)]

    assert len(cluster_by_domain(hits, per_domain=0, limit=4)) == 4


@pytest.mark.asyncio
async def test_the_service_returns_a_spread_of_domains(client, settings):
    from app.services.search_service import SearchService

    store = IndexStore(client, settings)
    await store.persist_batch(
        [
            PageDocument(
                url=f"https://a.gov.in/{i}", domain="a.gov.in", title=f"Karnataka health {i}",
                description="d", body_text=BODY, digest=f"cl-a{i}", brand_name="A",
            )
            for i in range(6)
        ]
        + [
            PageDocument(
                url="https://b.gov.in/1", domain="b.gov.in", title="Karnataka health b",
                description="d", body_text=BODY, digest="cl-b1", brand_name="B",
            )
        ]
    )

    service = SearchService(store, settings)
    payload = await service.search("karnataka health", limit=5)
    hosts = [item["url"].split("/")[2] for item in payload["results"]]

    assert hosts.count("a.gov.in") <= settings.max_results_per_domain
    assert "b.gov.in" in hosts


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_suggestion_is_a_word_not_a_title(client, settings):
    store = IndexStore(client, settings)
    await store.persist_batch(
        [
            PageDocument(
                url="https://a.gov.in/1", domain="a.gov.in",
                title="District Health Notifications for Every Office",
                description="d", body_text=BODY, digest="sg-1", brand_name="A",
            )
        ]
    )

    suggestions = await store.suggest("karn")

    assert suggestions == ["karnataka"]


@pytest.mark.asyncio
async def test_a_one_character_prefix_suggests_nothing(client, settings):
    store = IndexStore(client, settings)

    assert await store.suggest("k") == []


def test_the_same_page_under_www_is_not_a_second_result():
    """`ksrtc.in/` and `www.ksrtc.in/` are one page, not two results.

    The cap is per domain, so a homepage stored under both host forms used to
    fill two slots with the same content and the result page looked repeated.
    """
    hits = [Hit("https://ksrtc.in/"), Hit("https://www.ksrtc.in/"), Hit("https://ksrtc.in/fare")]

    assert urls(cluster_by_domain(hits, per_domain=2)) == [
        "https://ksrtc.in/",
        "https://ksrtc.in/fare",
    ]


def test_www_and_the_bare_host_share_one_per_domain_budget():
    """Three pages, two of them under `www.`: only two survive the cap."""
    hits = [
        Hit("https://ksrtc.in/"),
        Hit("https://www.ksrtc.in/fare"),
        Hit("https://www.ksrtc.in/routes"),
    ]

    assert len(cluster_by_domain(hits, per_domain=2)) == 2



def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def test_the_site_a_query_names_keeps_its_own_pages():
    """`phonepe` should show PhonePe's pages, not two of them and eight strangers.

    The per-domain cap is two, which is right for the open web and wrong when
    the reader typed the site's name: 183 of its pages are indexed and four
    reached the page.
    """
    hits = [Hit(u) for u in (
        "https://phonepe.com/payment-gateway",
        "https://phonepe.com/guides/payment-gateway",
        "https://phonepe.com/pricing",
        "https://phonepe.com/about",
        "https://phonepe.com/contact",
        "https://phonepe.com/careers",
        "https://other.in/x",
        "https://another.in/y",
    )]

    clustered = cluster_by_domain(
        hits, per_domain=2, limit=10, favoured="phonepe.com", favoured_per_domain=6
    )
    hosts = [_host(h.url) for h in clustered]

    assert hosts.count("phonepe.com") == 6
    assert "other.in" in hosts and "another.in" in hosts


def test_an_unrelated_query_keeps_the_tight_cap():
    hits = [Hit(u) for u in (
        "https://a.in/1", "https://a.in/2", "https://a.in/3", "https://b.in/4",
    )]

    clustered = cluster_by_domain(hits, per_domain=2, limit=10, favoured=None, favoured_per_domain=6)

    assert [_host(h.url) for h in clustered].count("a.in") == 2


def test_one_title_in_two_unicode_forms_is_one_result():
    """The same Bengali title arrives composed and decomposed from two URLs.

    Byte for byte they differ; to a reader they are the same words, and the
    result page showed both.
    """
    composed = "গোপনীয়তা নীতি"
    decomposed = "গোপনীয়তা নীতি"

    hits = [Hit(u) for u in ("https://a.in/1", "https://a.in/2", "https://a.in/3")]
    hits[0].title = composed
    hits[1].title = decomposed
    hits[2].title = "Privacy policy"

    clustered = cluster_by_domain(hits, per_domain=6)

    assert [h.title for h in clustered] == [composed, "Privacy policy"]


def test_the_same_page_in_two_languages_is_one_result():
    """`/bn/privacy-policy` and `/privacy-policy` are one page, shown twice.

    406 pages in the live index have such a twin. Folding the language segment
    for the result page merges the two cards without deleting either page -
    admission and compaction still see two URLs, so nothing is lost.
    """
    hits = [Hit(u) for u in (
        "https://phonepe.com/privacy-policy",
        "https://phonepe.com/bn/privacy-policy",
        "https://phonepe.com/hi/privacy-policy",
        "https://phonepe.com/payment-gateway",
    )]

    clustered = cluster_by_domain(hits, per_domain=6, limit=10)
    urls_kept = [h.url for h in clustered]

    assert urls_kept == [
        "https://phonepe.com/privacy-policy",
        "https://phonepe.com/payment-gateway",
    ]


def test_a_page_named_like_a_language_is_left_alone():
    """`/engineering` starts with `en` but is a page, not a translation."""
    hits = [Hit(u) for u in ("https://a.in/en", "https://a.in/engineering")]

    clustered = cluster_by_domain(hits, per_domain=6, limit=10)

    assert [h.url for h in clustered] == ["https://a.in/en", "https://a.in/engineering"]


def test_subdomains_of_one_site_share_its_cap():
    """`ir.phonepe.com` and `phonepe.com` are one site, not four.

    Keyed on the raw host, a brand's subdomains each got their own allowance and
    eight cards from one company filled a page with nothing from anywhere else.
    """
    hits = [Hit(u) for u in (
        "https://phonepe.com/payment-gateway",
        "https://ir.phonepe.com/investor-relations",
        "https://business.phonepe.com/articles/a",
        "https://terms-and-conditions.phonepe.com/general/list",
        "https://other.in/x",
    )]

    clustered = cluster_by_domain(hits, per_domain=2, limit=10)

    kept = [h.url for h in clustered]
    assert sum(1 for u in kept if "phonepe.com" in u) == 2
    assert "https://other.in/x" in kept


def test_a_multi_part_suffix_is_one_site():
    """`karnataka.gov.in` is one domain, not `gov.in`."""
    from app.agents import load_agent

    agent = load_agent("13_dedupe_compact_agent")

    assert agent._domain_of("https://karnataka.gov.in/health") == "karnataka.gov.in"
    assert agent._domain_of("https://ir.ctc.com/x") == "ctc.com"
    assert agent._domain_of("https://a.in/x") == "a.in"


def _brand_page(url: str, digest: str, brand: str) -> PageDocument:
    return PageDocument(
        url=url,
        domain=url.split("/")[2],
        title="phonepe",
        description="phonepe",
        body_text=BODY,
        digest=digest,
        brand_name=brand,
    )


@pytest.mark.asyncio
async def test_a_brand_query_still_finds_other_sites(client):
    """`phonepe` returns phonepe.com and nothing else, so the rest is fetched.

    One site owned every match for its own name: 183 phonepe.com pages, the
    word on all of them, and a hundred candidates that were all the same site.
    The other seats are now fetched with that site excluded.
    """
    store = IndexStore(client, Settings(environment="test"))
    pages = [
        _brand_page(f"https://phonepe.com/p{i}", f"brand-{i}", "PhonePe")
        for i in range(8)
    ] + [
        _brand_page("https://moneycontrol.com/phonepe-ipo", "other-1", "Moneycontrol"),
        _brand_page("https://business-standard.com/phonepe", "other-2", "Business Standard"),
    ]
    await store.persist_batch(pages)

    excluded = await store.search("phonepe", limit=20, exclude_domain="phonepe.com")

    assert excluded, "the excluded leg must still find the other sites"
    assert all("phonepe.com" not in hit.url for hit in excluded)
