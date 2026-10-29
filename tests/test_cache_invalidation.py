"""The search cache forgets only what a write made stale."""

from __future__ import annotations

from app.services.search_service import SearchCache


def cache_with(*payloads) -> SearchCache:
    cache = SearchCache()
    for index, urls in enumerate(payloads):
        cache.set((f"q{index}",), {"results": [{"url": url} for url in urls]})
    return cache


def test_a_write_drops_the_entries_that_mention_it():
    cache = cache_with(["https://a.in/1", "https://b.in/2"], ["https://c.in/3"])

    dropped = cache.invalidate_urls(["https://a.in/1"])

    assert dropped == 1
    assert cache.get(("q0",)) is None
    # The other query is untouched: expiring everything on every write would
    # throw away answers that are still correct.
    assert cache.get(("q1",)) is not None


def test_a_write_that_matches_nothing_drops_nothing():
    cache = cache_with(["https://a.in/1"])

    assert cache.invalidate_urls(["https://z.in/9"]) == 0
    assert cache.get(("q0",)) is not None


def test_an_empty_list_is_a_no_op():
    cache = cache_with(["https://a.in/1"])

    assert cache.invalidate_urls([]) == 0
    assert cache.get(("q0",)) is not None


def test_an_entry_matching_two_urls_is_dropped_once():
    cache = cache_with(["https://a.in/1", "https://a.in/2"])

    assert cache.invalidate_urls(["https://a.in/1", "https://a.in/2"]) == 1


def test_the_hit_and_miss_counters_still_work_after_invalidation():
    cache = cache_with(["https://a.in/1"])
    cache.invalidate_urls(["https://a.in/1"])

    assert cache.get(("q0",)) is None
    assert cache.misses == 1


def test_an_entry_with_no_results_is_left_alone():
    cache = SearchCache()
    cache.set(("q",), {"results": []})

    assert cache.invalidate_urls(["https://a.in/1"]) == 0
    assert cache.get(("q",)) is not None
