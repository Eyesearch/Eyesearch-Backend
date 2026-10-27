"""The baseline workflow: one queue row in, one page row out.

These tests exercise the isolated path only — `crawl_queue` -> fetch -> basic
parse -> `pages` -> queue status — with the network stubbed out. The five
agents that are decoupled from the active path are covered by their own suites.
"""

from __future__ import annotations

import pytest

from app import database
from app.engine.config import EngineSettings
from app.indexer import scheduler
from app.indexer.baseline import count_pages, run_baseline_pass

PAGE_HTML = """
<html>
  <head>
    <title>Election Commission of India</title>
    <meta name="description" content="The Election Commission of India conducts free and fair elections." />
  </head>
  <body>
    <h1>Election Commission of India</h1>
    <p>The Election Commission of India is an autonomous constitutional authority
       responsible for administering election processes in India.</p>
    <a href="/voters">Voters portal</a>
  </body>
</html>
"""


def _settings(**overrides) -> EngineSettings:
    return EngineSettings(crawl_delay_seconds=0.0, **overrides)


async def _enqueue(url: str, domain: str, category: str = "government") -> None:
    await database.execute_safe(
        "INSERT OR REPLACE INTO crawl_queue (url, domain, category, depth, reason, status) "
        "VALUES (?, ?, ?, 0, 'test', 'pending')",
        [url, domain, category],
    )


async def _queue_status(url: str) -> str:
    result = await database.execute_safe("SELECT status FROM crawl_queue WHERE url = ?", [url])
    rows = getattr(result, "rows", ()) or ()
    return str(rows[0][0]) if rows else ""


async def _fake_fetch(client, url, settings):
    return PAGE_HTML, 200


@pytest.mark.asyncio
async def test_baseline_pass_writes_one_page_and_completes_the_queue_row(temp_db) -> None:
    url = "https://eci.gov.in/"
    await _enqueue(url, "eci.gov.in")
    assert await count_pages() == 0

    report = await run_baseline_pass(settings=_settings(), fetch=_fake_fetch)

    assert report["attempted"] == 1
    assert report["processed"] == 1
    assert report["failed"] == 0
    assert await count_pages() == 1
    assert await _queue_status(url) == "processed"

    page = await database.execute_safe("SELECT title, kind FROM pages WHERE url = ?", [url])
    rows = getattr(page, "rows", ()) or ()
    assert rows, "the page row must exist"
    assert "Election Commission" in str(rows[0][0])
    assert rows[0][1] == "root"

    site = await database.execute_safe("SELECT domain FROM sites WHERE domain = ?", ["eci.gov.in"])
    assert getattr(site, "rows", ()), "the site row must be written with the page"


@pytest.mark.asyncio
async def test_a_failed_fetch_marks_the_row_failed_and_writes_nothing(temp_db) -> None:
    url = "https://ssc.nic.in/"
    await _enqueue(url, "ssc.nic.in")

    async def _dead_fetch(client, url, settings):
        return None, None

    report = await run_baseline_pass(settings=_settings(), fetch=_dead_fetch)

    assert report["processed"] == 0
    assert report["failed"] == 1
    assert await count_pages() == 0
    assert await _queue_status(url) == "failed"


@pytest.mark.asyncio
async def test_a_parse_rejection_of_a_sitelink_marks_the_row_failed(temp_db) -> None:
    url = "https://example.gov.in/voters"
    await _enqueue(url, "example.gov.in")

    async def _empty_fetch(client, url, settings):
        return "<html><head><title>x</title></head><body>short</body></html>", 200

    def _rejecting_parse(url, html, status, category):
        return None, "too_short"

    report = await run_baseline_pass(
        settings=_settings(), fetch=_empty_fetch, parse=_rejecting_parse
    )

    assert report["processed"] == 0
    assert report["failed"] == 1
    assert report["reasons"] == {"too_short": 1}
    assert await _queue_status(url) == "failed"


@pytest.mark.asyncio
async def test_a_rejected_root_still_writes_a_hostname_card(temp_db) -> None:
    """A root that answered 200 must prove a write, not only a rejection."""
    url = "https://ksrtc.karnataka.gov.in/"
    await _enqueue(url, "ksrtc.karnataka.gov.in")

    async def _js_only_fetch(client, url, settings):
        # A JavaScript shell: served, but with almost nothing to extract.
        return "<html><head><title></title></head><body><div id='root'></div></body></html>", 200

    report = await run_baseline_pass(settings=_settings(), fetch=_js_only_fetch)

    assert report["processed"] == 1
    assert await count_pages() == 1
    assert await _queue_status(url) == "processed"
    page = await database.execute_safe("SELECT title FROM pages WHERE url = ?", [url])
    rows = getattr(page, "rows", ()) or ()
    assert rows and "ksrtc" in str(rows[0][0]).lower()


@pytest.mark.asyncio
async def test_an_empty_queue_is_reported_without_touching_pages(temp_db) -> None:
    report = await run_baseline_pass(settings=_settings(), fetch=_fake_fetch)

    assert report.get("empty") is True
    assert report["attempted"] == 0
    assert await count_pages() == 0


def test_baseline_only_stops_the_other_workers() -> None:
    cfg = EngineSettings(baseline_only=True)
    assert scheduler.baseline_only(cfg) is True
    # Every other worker refuses to start while the baseline path is active.
    assert scheduler.start_url_queue_worker(cfg) is None
    assert scheduler.start_discovery_worker(cfg) is None
    assert scheduler.start_seed_crawler(cfg) is None
    assert scheduler.start_backlog_worker(cfg) is None


def test_the_full_pipeline_is_restorable_by_flag() -> None:
    assert EngineSettings(baseline_only=False).baseline_only is False
    assert scheduler.baseline_only(EngineSettings(baseline_only=False)) is False
    # With the flag off, the baseline worker stays out of the way.
    assert scheduler.start_baseline_worker(EngineSettings(baseline_only=False)) is None
