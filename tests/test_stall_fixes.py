"""The three surgical stall fixes: re-arm, dynamic sleep, loud status updates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import database
from app.config import Settings
from app.engine.config import EngineSettings
from app.engine.store import REARMABLE_STATUSES, get_store, _older_than
from app.indexer import scheduler


@pytest.fixture()
async def temp_db(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/stall.db")
    await database.connect(settings)
    await database.initialize_schema()
    yield get_store()
    await database.close()


def _stamp(hours_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


# --- 1. re-arm -----------------------------------------------------------

def test_the_rearmable_states_are_processed_and_failed() -> None:
    assert REARMABLE_STATUSES == {"processed", "failed"}


def test_older_than_compares_sqlite_timestamps() -> None:
    assert _older_than(_stamp(7), 6 * 3600) is True
    assert _older_than(_stamp(1), 6 * 3600) is False
    assert _older_than(None, 6 * 3600) is True, "a missing timestamp is always re-armable"
    assert _older_than("not a timestamp", 6 * 3600) is True


async def test_a_processed_domain_is_rearmed_after_the_cooldown(temp_db) -> None:
    store = temp_db
    assert await store.enqueue_domain("eci.gov.in") is True
    await store.mark_domain_processed("eci.gov.in")

    # inside the cooldown: not re-queued
    assert await store.enqueue_domain("eci.gov.in") is False

    # age it past the cooldown
    await database.execute_safe(
        "UPDATE crawl_backlog SET processed_at = ? WHERE query = 'eci.gov.in'", [_stamp(7)]
    )

    assert await store.enqueue_domain("eci.gov.in") is True, "a stale domain must re-arm"
    assert await store.pending_domains(5) == ["eci.gov.in"]


async def test_a_failed_domain_is_rearmed_too(temp_db) -> None:
    store = temp_db
    await store.enqueue_domain("ssc.nic.in")
    await store.mark_domain_processed("ssc.nic.in", status="failed")
    await database.execute_safe(
        "UPDATE crawl_backlog SET processed_at = ? WHERE query = 'ssc.nic.in'", [_stamp(9)]
    )

    assert await store.enqueue_domain("ssc.nic.in") is True
    rows = await database.execute_safe(
        "SELECT status FROM crawl_backlog WHERE query = 'ssc.nic.in'"
    )
    assert str(rows.rows[0][0]) == "pending"


async def test_a_pending_domain_is_never_requeued(temp_db) -> None:
    store = temp_db
    await store.enqueue_domain("tn.gov.in")
    assert await store.enqueue_domain("tn.gov.in") is False


async def test_the_rearm_lets_the_refill_keep_working(temp_db, monkeypatch) -> None:
    """The point of the fix: after the registry is exhausted, refill still adds."""
    from app.indexer import crawler as pipeline
    from app.agents import FrontierAgent

    store = temp_db
    await store.enqueue_domain("eci.gov.in")
    await store.mark_domain_processed("eci.gov.in")
    await database.execute_safe(
        "UPDATE crawl_backlog SET processed_at = ? WHERE query = 'eci.gov.in'", [_stamp(8)]
    )

    async def _no_harvest(self, host, *, fetcher=None):
        return []

    monkeypatch.setattr(FrontierAgent, "harvest", _no_harvest)
    monkeypatch.setattr(
        pipeline, "seeds_for",
        lambda categories=None: [pipeline.CategorySeed("https://eci.gov.in", "ECI", "government")],
    )

    report = await pipeline.refill_frontier(
        target=5, store=store,
        settings=EngineSettings(frontier_low_water=20, harvest_hosts_per_pass=1),
    )

    assert report["seeds"] == 1, "the stale seed re-entered the queue"
    assert await store.pending_domains(5) == ["eci.gov.in"]


# --- 2. dynamic backlog sleep -------------------------------------------

def test_a_full_batch_retries_after_thirty_seconds() -> None:
    settings = EngineSettings(backlog_batch_queries=3, backlog_interval_seconds=900)
    assert scheduler._backlog_sleep_seconds({"queries": 3}, settings) == 30.0
    assert scheduler._backlog_sleep_seconds({"queries": 5}, settings) == 30.0


def test_a_partial_batch_retries_sooner_than_the_full_interval() -> None:
    settings = EngineSettings(backlog_batch_queries=3, backlog_interval_seconds=900)
    assert scheduler._backlog_sleep_seconds({"queries": 1}, settings) == 60.0


def test_an_empty_batch_uses_the_full_interval() -> None:
    settings = EngineSettings(backlog_batch_queries=3, backlog_interval_seconds=900)
    seconds = scheduler._backlog_sleep_seconds({"queries": 0}, settings)
    assert 900 <= seconds <= 930


# --- 3. loud status updates ---------------------------------------------

async def test_marking_an_unknown_domain_warns(temp_db, caplog) -> None:
    store = temp_db
    with caplog.at_level("WARNING"):
        await store.mark_domain_processed("never-queued.gov.in")

    assert any("matched no row for never-queued.gov.in" in r.getMessage() for r in caplog.records)


async def test_a_successful_mark_is_quiet(temp_db, caplog) -> None:
    store = temp_db
    await store.enqueue_domain("eci.gov.in")

    with caplog.at_level("WARNING"):
        await store.mark_domain_processed("eci.gov.in")

    assert not [r for r in caplog.records if "matched no row" in r.getMessage()]
    assert await store.pending_domains(5) == []
