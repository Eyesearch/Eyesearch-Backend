"""Early warning when the frontier grows while indexing stalls."""

from __future__ import annotations

from app.orchestrator import FRONTIER_STALL_WINDOW, FrontierGrowthWatch


def _run(watch, batches):
    """Feed (indexed, discovered, closed) tuples; return the warnings raised."""
    warnings = []
    for indexed, discovered, closed in batches:
        message = watch.observe(indexed=indexed, discovered=discovered, closed=closed)
        if message:
            warnings.append(message)
    return warnings


def test_no_warning_before_the_window_is_full():
    watch = FrontierGrowthWatch(window=10)
    warnings = _run(watch, [(0, 50, 0)] * 9)
    assert warnings == []


def test_a_growing_frontier_with_no_indexing_warns():
    watch = FrontierGrowthWatch(window=3)
    warnings = _run(watch, [(0, 50, 0)] * 3)

    assert len(warnings) == 1
    assert "frontier growing while indexing is stalled" in warnings[0]
    assert "150 discovered" in warnings[0]
    assert "0 pages indexed over 3 batches" in warnings[0]


def test_progress_suppresses_the_warning():
    watch = FrontierGrowthWatch(window=3)
    # Discoveries still outpace closures, but pages are reaching the index.
    warnings = _run(watch, [(4, 50, 4)] * 3)
    assert warnings == []


def test_a_shrinking_frontier_does_not_warn():
    watch = FrontierGrowthWatch(window=3)
    # Nothing indexed, but the queue is draining faster than it fills.
    warnings = _run(watch, [(0, 2, 40)] * 3)
    assert warnings == []


def test_an_idle_crawler_does_not_warn():
    watch = FrontierGrowthWatch(window=3)
    warnings = _run(watch, [(0, 0, 0)] * 3)
    assert warnings == [], "no growth and no indexing is idle, not stalled"


def test_the_warning_repeats_once_per_window_not_once_per_batch():
    watch = FrontierGrowthWatch(window=3)
    warnings = _run(watch, [(0, 50, 0)] * 9)
    assert len(warnings) == 3, "one warning per window, so a stall is not a log flood"


def test_the_window_defaults_to_the_module_constant():
    assert FRONTIER_STALL_WINDOW >= 5
    watch = FrontierGrowthWatch()
    assert _run(watch, [(0, 10, 0)] * (FRONTIER_STALL_WINDOW - 1)) == []
    assert len(_run(watch, [(0, 10, 0)])) == 1
