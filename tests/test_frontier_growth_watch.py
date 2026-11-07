"""Early warning when the frontier grows while indexing stalls."""

from __future__ import annotations

from app.orchestrator import (
    FRONTIER_STALL_WINDOW,
    IDLE_QUEUE_STALL_BATCHES,
    FrontierGrowthWatch,
    IdleQueueWatch,
)


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


# ---------------------------------------------------------------------------
# An empty frontier with a full queue is a stall, not an idle crawler
# ---------------------------------------------------------------------------


def _idle(watch, empty_batches):
    """Feed booleans; return how many times the watch raised the alarm."""
    return sum(1 for empty in empty_batches if watch.observe(empty_batch=empty))


def test_an_empty_queue_is_not_reported_before_the_window():
    watch = IdleQueueWatch(window=5)
    assert _idle(watch, [True] * 4) == 0


def test_a_full_window_of_empty_batches_is_a_stall():
    watch = IdleQueueWatch(window=5)
    assert _idle(watch, [True] * 5) == 1


def test_a_batch_with_work_resets_the_count():
    watch = IdleQueueWatch(window=3)
    # Two empty batches, then real work: the run is not stuck, it was quiet.
    assert _idle(watch, [True, True, False, True, True]) == 0


def test_the_alarm_repeats_once_per_window():
    watch = IdleQueueWatch(window=2)
    assert _idle(watch, [True] * 6) == 3


def test_the_idle_window_defaults_to_the_module_constant():
    assert IDLE_QUEUE_STALL_BATCHES >= 5
    watch = IdleQueueWatch()
    assert _idle(watch, [True] * (IDLE_QUEUE_STALL_BATCHES - 1)) == 0
    assert _idle(watch, [True]) == 1
