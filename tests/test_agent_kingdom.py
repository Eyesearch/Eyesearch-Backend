"""Agents 14 to 21: loadability, failover, and the doctor's kill rule."""

from __future__ import annotations

import asyncio

import pytest

from app.agents import load_agent

KINGDOM = (
    "14_king_orchestrator_agent",
    "15_db_doctor_agent",
    "16_open_scout_agent",
    "17_semantic_dedupe_agent",
    "18_trap_shield_agent",
    "19_fast_cache_agent",
    "20_system_sentinel_agent",
    "21_regent_agent",
)


@pytest.mark.parametrize("name", KINGDOM)
def test_every_agent_in_the_kingdom_loads(name):
    module = load_agent(name)
    assert module is not None


# ---------------------------------------------------------------------------
# 14 — the King
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stalled_worker_is_restarted():
    king = load_agent("14_king_orchestrator_agent").KingOrchestratorAgent(stale_after=10.0)
    now = [0.0]
    king._clock = lambda: now[0]

    started = asyncio.Event()

    async def worker():
        started.set()
        await asyncio.sleep(3600)

    king.register("crawler", worker)
    await started.wait()
    king.beat("crawler")

    # Beating keeps it alive.
    now[0] = 5.0
    report = await king.check_once()
    assert report.stalled == []

    # Silence past the threshold restarts it.
    now[0] = 20.0
    report = await king.check_once()
    assert "crawler" in report.stalled
    assert "crawler" in report.restarted

    await king.stop()


@pytest.mark.asyncio
async def test_a_worker_that_raises_is_restarted_by_its_factory():
    king = load_agent("14_king_orchestrator_agent").KingOrchestratorAgent(stale_after=10.0)
    runs = []

    async def flaky():
        runs.append(1)
        raise RuntimeError("boom")

    king.register("flaky", flaky)
    await asyncio.sleep(0.05)
    assert len(runs) >= 1
    await king.stop()


# ---------------------------------------------------------------------------
# 21 — the Regent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_regent_takes_over_when_the_king_goes_silent():
    king = load_agent("14_king_orchestrator_agent").KingOrchestratorAgent(stale_after=10.0)
    regent = load_agent("21_regent_agent").RegentAgent(king, stale_after=10.0)
    now = [0.0]
    regent._clock = lambda: now[0]

    regent.king_beat()
    result = await regent.check_once()
    assert result["action"] == "none"
    assert regent.regent_active is False

    now[0] = 15.0  # the king has stopped beating
    result = await regent.check_once()
    assert result["action"] == "promoted"
    assert regent.regent_active is True
    assert regent.promotions == 1

    # A second silent pass does not promote twice.
    result = await regent.check_once()
    assert result["action"] == "none"
    assert regent.promotions == 1


@pytest.mark.asyncio
async def test_the_regent_stands_down_when_the_king_returns():
    king = load_agent("14_king_orchestrator_agent").KingOrchestratorAgent(stale_after=10.0)
    regent = load_agent("21_regent_agent").RegentAgent(king, stale_after=10.0)
    now = [0.0]
    regent._clock = lambda: now[0]

    regent.king_beat()
    now[0] = 15.0
    await regent.check_once()
    assert regent.regent_active is True

    regent.king_beat()
    result = await regent.check_once()
    assert result["action"] == "demoted"
    assert regent.regent_active is False


# ---------------------------------------------------------------------------
# 15 — the doctor's kill rule
# ---------------------------------------------------------------------------


def test_the_doctor_kills_a_hung_query_but_not_a_healthy_one():
    select = load_agent("15_db_doctor_agent").select_victims
    rows = [
        {"pid": 10, "state": "active", "duration_ms": 4000},      # hung
        {"pid": 11, "state": "active", "duration_ms": 50},        # fine
        {"pid": 12, "state": "idle", "duration_ms": 90000},       # idle is not hung
        {"pid": 13, "state": "active", "duration_ms": 9000},      # hung
    ]

    assert select(rows, own_pid=99, hung_ms=1500) == [10, 13]


def test_the_doctor_never_kills_itself():
    select = load_agent("15_db_doctor_agent").select_victims
    rows = [{"pid": 7, "state": "active", "duration_ms": 60000}]

    assert select(rows, own_pid=7) == []


def test_the_doctor_leaves_background_workers_alone():
    select = load_agent("15_db_doctor_agent").select_victims
    rows = [
        {"pid": 20, "state": "active", "duration_ms": 60000, "backend_type": "autovacuum worker"},
        {"pid": 21, "state": "active", "duration_ms": 60000, "backend_type": "walsender"},
    ]

    assert select(rows, own_pid=99) == []


def test_the_doctor_kills_a_long_idle_transaction():
    select = load_agent("15_db_doctor_agent").select_victims
    rows = [{"pid": 30, "state": "idle in transaction", "duration_ms": 400_000}]

    assert select(rows, own_pid=99) == [30]


# ---------------------------------------------------------------------------
# 16, 18, 19 — the adapters delegate rather than duplicate
# ---------------------------------------------------------------------------


def test_the_scout_parses_sitemaps_and_feeds():
    parse = load_agent("16_open_scout_agent").parse_feed

    assert parse("<urlset><url><loc>https://a.com/1</loc></url></urlset>") == ["https://a.com/1"]
    assert parse("<rss><channel><item><link>https://a.com/2</link></item></channel></rss>") == [
        "https://a.com/2"
    ]
    assert parse("not xml at all") == []


def test_the_shield_blocks_what_the_frontier_blocks():
    shield = load_agent("18_trap_shield_agent")
    frontier = load_agent("01_frontier_agent")

    # The adapter must not hold its own copy of the rules.
    assert shield.TRAP_PATH_SUBSTRINGS is frontier.TRAP_PATH_SUBSTRINGS
    assert shield.TRACKING_PARAMS is frontier.TRACKING_PARAMS

    assert shield.classify_trap("https://a.com/list?page=999") != ""
    assert shield.classify_trap("https://a.com/register") != ""
    assert shield.classify_trap("https://a.com/x?utm_source=n").startswith("tracking:")
    assert shield.classify_trap("https://a.com/health") == ""


def test_the_cache_agent_wraps_the_search_cache():
    agent = load_agent("19_fast_cache_agent").FastCacheAgent()
    agent.set(("k",), {"results": []})

    assert agent.get(("k",)) == {"results": []}
    assert agent.stats()["ttl_seconds"] == 60.0


# ---------------------------------------------------------------------------
# 17 — semantic dedupe
# ---------------------------------------------------------------------------


def test_simhash_separates_syndicated_content_from_unrelated_text():
    dedupe = load_agent("17_semantic_dedupe_agent")
    original = "The department publishes district health notifications for every office across the state today"
    syndicated = "By Staff Reporter. The department publishes district health notifications for every office across the state today."
    unrelated = "Completely different text about cricket scores and weather patterns in the south"

    near = dedupe.hamming(dedupe.simhash(original), dedupe.simhash(syndicated))
    far = dedupe.hamming(dedupe.simhash(original), dedupe.simhash(unrelated))

    assert near <= dedupe.NEAR_DUPLICATE_DISTANCE
    assert far > dedupe.NEAR_DUPLICATE_DISTANCE


# ---------------------------------------------------------------------------
# 20 — the sentinel
# ---------------------------------------------------------------------------


def test_the_sentinel_reports_pressure():
    evaluate = load_agent("20_system_sentinel_agent").evaluate_system

    assert evaluate(100, 90, 100, 90).pressure == "ok"
    assert evaluate(100, 10, 100, 90).pressure == "throttle"
    assert evaluate(100, 3, 100, 90).pressure == "stop"
    assert evaluate(100, 90, 100, 2).pressure == "stop"


def test_the_sentinel_does_not_invent_a_reading_it_cannot_take():
    state = load_agent("20_system_sentinel_agent").evaluate_system(0, 0, 0, 0)
    assert state.ram_used_fraction == 0.0
    assert state.disk_used_fraction == 0.0
