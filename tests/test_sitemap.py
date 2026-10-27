"""Sitemap and robots.txt harvesting, and the refill that uses it."""

from __future__ import annotations

import importlib

import pytest

from app import database
from app.agents import FrontierAgent, FrontierConfig
from app.config import Settings
from app.engine.config import EngineSettings
from app.engine.store import get_store

sitemap = importlib.import_module("app.agents.01_frontier_agent.sitemap")

ROBOTS = """
User-agent: *
Disallow: /admin
Sitemap: https://eci.gov.in/sitemap.xml
sitemap: /sitemap-news.xml
"""

URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://eci.gov.in/voters</loc></url>
  <url><loc>https://eci.gov.in/forms/registration</loc></url>
  <url><loc>https://eci.gov.in/report.pdf</loc></url>
  <url><loc>https://eci.gov.in/assets/logo.png</loc></url>
  <url><loc>https://elsewhere.gov.in/other</loc></url>
</urlset>
"""

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://eci.gov.in/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>https://eci.gov.in/sitemap-news.xml</loc></sitemap>
</sitemapindex>
"""


@pytest.fixture()
async def temp_db(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/sitemap.db")
    await database.connect(settings)
    await database.initialize_schema()
    yield get_store()
    await database.close()


# --- parsing --------------------------------------------------------------

def test_robots_sitemap_directives_are_made_absolute() -> None:
    found = sitemap.sitemap_urls_from_robots(ROBOTS, "https://eci.gov.in/")
    assert found == ["https://eci.gov.in/sitemap.xml", "https://eci.gov.in/sitemap-news.xml"]


def test_a_robots_file_without_sitemaps_yields_nothing() -> None:
    assert sitemap.sitemap_urls_from_robots("User-agent: *\nDisallow: /\n", "https://x.gov.in/") == []


def test_assets_are_filtered_and_scope_is_enforced() -> None:
    assert sitemap.is_content_url("https://eci.gov.in/voters", host="eci.gov.in")
    assert not sitemap.is_content_url("https://eci.gov.in/report.pdf", host="eci.gov.in")
    assert not sitemap.is_content_url("https://eci.gov.in/app.js", host="eci.gov.in")
    assert not sitemap.is_content_url("https://elsewhere.gov.in/x", host="eci.gov.in")
    assert not sitemap.is_content_url("mailto:someone@eci.gov.in", host="eci.gov.in")


def test_a_urlset_yields_only_content_urls_on_the_host() -> None:
    urls = sitemap.harvest_urls([("s", URLSET)], host="eci.gov.in")
    assert urls == ["https://eci.gov.in/voters", "https://eci.gov.in/forms/registration"]


def test_a_nested_sitemap_index_is_followed() -> None:
    children = list(sitemap.iter_sitemap_index(INDEX))
    assert children == [
        "https://eci.gov.in/sitemap-pages.xml",
        "https://eci.gov.in/sitemap-news.xml",
    ]


def test_parsing_is_capped_and_never_loads_everything() -> None:
    big = "<urlset>" + "".join(
        f"<url><loc>https://eci.gov.in/page{i}</loc></url>" for i in range(5000)
    ) + "</urlset>"

    capped = sitemap.harvest_urls([("s", big)], host="eci.gov.in", limit=25)
    assert len(capped) == 25
    assert capped[0].endswith("/page0")


def test_invalid_xml_is_survivable() -> None:
    assert sitemap.harvest_urls([("s", "<urlset><url><loc>https://eci.gov.in/x")], host="eci.gov.in") == []
    assert sitemap.looks_like_sitemap("not xml at all") is False


# --- the frontier agent's harvest ----------------------------------------

async def test_harvest_reads_robots_then_the_sitemap(temp_db) -> None:
    calls: list[str] = []

    class _Result:
        def __init__(self, html, status=200):
            self.html = html
            self.status = status

        @property
        def ok(self):
            return self.html is not None and self.status < 400

    class _Fetcher:
        async def fetch(self, url):
            calls.append(url)
            if url.endswith("robots.txt"):
                return _Result(ROBOTS)
            if url.endswith("sitemap.xml"):
                return _Result(URLSET)
            return _Result(None, 404)

        async def aclose(self):
            return None

    frontier = FrontierAgent(store=temp_db, config=FrontierConfig())
    urls = await frontier.harvest("eci.gov.in", fetcher=_Fetcher())

    assert urls == ["https://eci.gov.in/voters", "https://eci.gov.in/forms/registration"]
    assert calls[0].endswith("robots.txt")


async def test_harvest_falls_back_to_sitemap_xml_when_robots_has_none(temp_db) -> None:
    calls: list[str] = []

    class _Result:
        def __init__(self, html, status=200):
            self.html = html
            self.status = status

        @property
        def ok(self):
            return self.html is not None and self.status < 400

    class _Fetcher:
        async def fetch(self, url):
            calls.append(url)
            if url.endswith("robots.txt"):
                return _Result("User-agent: *\nDisallow: /admin\n")
            if url.endswith("/sitemap.xml"):
                return _Result(URLSET)
            return _Result(None, 404)

        async def aclose(self):
            return None

    frontier = FrontierAgent(store=temp_db, config=FrontierConfig())
    urls = await frontier.harvest("eci.gov.in", fetcher=_Fetcher())

    assert urls, "the standard path must be tried"
    assert any(url.endswith("/sitemap.xml") for url in calls)


async def test_a_missing_sitemap_is_not_an_error(temp_db) -> None:
    class _Fetcher:
        async def fetch(self, url):
            raise RuntimeError("connection reset")

        async def aclose(self):
            return None

    frontier = FrontierAgent(store=temp_db, config=FrontierConfig())
    assert await frontier.harvest("broken.gov.in", fetcher=_Fetcher()) == []


# --- refill uses the harvest ---------------------------------------------

async def test_refill_pulls_sitemap_urls_into_the_queue(temp_db, monkeypatch) -> None:
    from app.indexer import crawler as pipeline

    store = temp_db
    await store.ingest_documents(
        [{"url": "https://eci.gov.in/", "canonical_url": "https://eci.gov.in/",
          "title": "Election Commission of India",
          "description": "Conducts elections in India.", "payload_bytes": 120}]
    )

    async def _fake_harvest(self, host, *, fetcher=None):
        return [f"https://{host}/page{i}" for i in range(40)]

    monkeypatch.setattr(FrontierAgent, "harvest", _fake_harvest)
    monkeypatch.setattr(
        pipeline, "seeds_for", lambda categories=None: []
    )  # registry drained

    report = await pipeline.refill_frontier(
        target=20, store=store, settings=EngineSettings(harvest_hosts_per_pass=1)
    )

    assert report["after"] >= 20
    assert report["links"] >= 0
    queued = await database.execute_safe(
        "SELECT url FROM crawl_queue WHERE reason = 'sitemap' LIMIT 5"
    )
    assert getattr(queued, "rows", ()), "sitemap urls must reach crawl_queue"


async def test_refill_never_requeues_a_known_url(temp_db, monkeypatch) -> None:
    from app.indexer import crawler as pipeline

    store = temp_db
    await store.queue_url("https://eci.gov.in/voters", domain="eci.gov.in", reason="sitemap")
    await store.ingest_documents(
        [{"url": "https://eci.gov.in/", "canonical_url": "https://eci.gov.in/",
          "title": "Election Commission", "description": "Elections.", "payload_bytes": 90}]
    )

    async def _fake_harvest(self, host, *, fetcher=None):
        return ["https://eci.gov.in/voters", "https://eci.gov.in/new-page"]

    monkeypatch.setattr(FrontierAgent, "harvest", _fake_harvest)
    monkeypatch.setattr(pipeline, "seeds_for", lambda categories=None: [])

    await pipeline.refill_frontier(target=10, store=store, settings=EngineSettings(harvest_hosts_per_pass=1))

    rows = await database.execute_safe(
        "SELECT url FROM crawl_queue WHERE url = 'https://eci.gov.in/voters'"
    )
    assert len(getattr(rows, "rows", ())) == 1, "no duplicate queue entry"


# --- gzip sitemaps --------------------------------------------------------

def test_a_gzipped_urlset_is_decompressed_and_parsed() -> None:
    import gzip

    payload = gzip.compress(URLSET.encode("utf-8"))
    assert sitemap.is_gzip_payload(payload) is True
    assert sitemap.looks_like_sitemap(payload) is True

    urls = sitemap.harvest_urls([("https://eci.gov.in/sitemap.xml.gz", payload)], host="eci.gov.in")
    assert urls == ["https://eci.gov.in/voters", "https://eci.gov.in/forms/registration"]


def test_a_gzipped_index_is_followed() -> None:
    import gzip

    payload = gzip.compress(INDEX.encode("utf-8"))
    children = list(sitemap.iter_sitemap_index(payload, url="https://eci.gov.in/sitemap.xml.gz"))
    assert children == [
        "https://eci.gov.in/sitemap-pages.xml",
        "https://eci.gov.in/sitemap-news.xml",
    ]


def test_decompression_streams_rather_than_buffering_everything() -> None:
    import gzip

    big = "<urlset>" + "".join(
        f"<url><loc>https://eci.gov.in/p{i}</loc></url>" for i in range(20000)
    ) + "</urlset>"
    payload = gzip.compress(big.encode("utf-8"))

    stream = sitemap.open_stream(payload, url="https://eci.gov.in/sitemap.xml.gz")
    assert stream is not None
    # the stream is lazy: nothing is inflated until it is read
    first = stream.read(64)
    assert first.startswith("<?xml") or first.startswith("<urlset")
    urls = sitemap.harvest_urls([("s.xml.gz", payload)], host="eci.gov.in", limit=10)
    assert len(urls) == 10


def test_a_corrupt_gzip_member_is_survivable() -> None:
    broken = b"\x1f\x8b\x08\x00not-really-gzip"
    assert sitemap.harvest_urls([("s.xml.gz", broken)], host="eci.gov.in") == []


def test_the_standard_paths_include_the_gzipped_forms() -> None:
    assert "/sitemap.xml.gz" in sitemap.STANDARD_SITEMAP_PATHS
    assert "/sitemap_index.xml.gz" in sitemap.STANDARD_SITEMAP_PATHS


async def test_harvest_uses_raw_bytes_for_a_gzipped_sitemap(temp_db) -> None:
    import gzip

    class _Fetcher:
        async def fetch(self, url):
            class R:
                html = "User-agent: *\nSitemap: https://eci.gov.in/sitemap.xml.gz\n"
                status = 200

                @property
                def ok(self):
                    return True

            return R()

        async def fetch_raw(self, url):
            if url.endswith(".gz"):
                return gzip.compress(URLSET.encode("utf-8")), 200
            return None, 404

        async def aclose(self):
            return None

    frontier = FrontierAgent(store=temp_db, config=FrontierConfig())
    urls = await frontier.harvest("eci.gov.in", fetcher=_Fetcher())

    assert urls == ["https://eci.gov.in/voters", "https://eci.gov.in/forms/registration"]


async def test_a_thin_queue_triggers_the_aggressive_harvest(temp_db, monkeypatch) -> None:
    from app.indexer import crawler as pipeline

    store = temp_db
    for i in range(6):
        await store.ingest_documents(
            [{"url": f"https://site{i}.gov.in/", "canonical_url": f"https://site{i}.gov.in/",
              "title": f"Portal {i}", "description": "Official portal.", "payload_bytes": 90}]
        )

    harvested_hosts: list[str] = []

    async def _fake_harvest(self, host, *, fetcher=None):
        harvested_hosts.append(host)
        return [f"https://{host}/page{i}" for i in range(30)]

    monkeypatch.setattr(FrontierAgent, "harvest", _fake_harvest)
    monkeypatch.setattr(pipeline, "seeds_for", lambda categories=None: [])

    report = await pipeline.refill_frontier(
        target=120,
        store=store,
        settings=EngineSettings(
            frontier_refill_trigger=50, harvest_hosts_per_pass=1,
            harvest_hosts_per_pass_aggressive=4,
        ),
    )

    assert report["aggressive"] is True
    assert len(harvested_hosts) >= 2, "aggressive mode inspects several domains"
    assert report["after"] >= 100


# --- no tight-loop polling -------------------------------------------------

async def test_the_discovery_pass_stops_when_the_same_domains_come_back(temp_db, monkeypatch) -> None:
    """A domain that is not marked done must not spin the pass."""
    from app.indexer import crawler as pipeline
    from app.orchestrator import Orchestrator

    store = temp_db
    calls = {"batches": 0}

    async def _always_same(self, limit=None):
        return ["stuck.gov.in"]

    async def _never_done(self, host, *, status="processed"):
        return None  # pretend the status update failed

    async def _run_domains(self, domains, **kwargs):
        calls["batches"] += 1
        return {"domains": len(domains), "indexed_pages": 0, "sites": 0, "accepted": 0,
                "errors": {}, "reports": {}}

    monkeypatch.setattr(FrontierAgent, "next_batch", _always_same)
    monkeypatch.setattr(FrontierAgent, "mark_done", _never_done)
    monkeypatch.setattr(Orchestrator, "run_domains", _run_domains)

    report = await pipeline.run_discovery_pass(
        store=store,
        settings=EngineSettings(discovery_batch_size=1, discovery_batch_pause_seconds=0.0,
                                worker_idle_backoff_seconds=1),
    )

    assert calls["batches"] == 1, "the repeat must end the pass, not loop"
    assert report["batches"] == 1


async def test_the_discovery_pass_is_capped_per_run(temp_db, monkeypatch) -> None:
    from app.indexer import crawler as pipeline
    from app.orchestrator import Orchestrator

    store = temp_db
    counter = {"n": 0}

    async def _fresh_batch(self, limit=None):
        counter["n"] += 1
        return [f"site{counter['n']}.gov.in"]

    async def _run_domains(self, domains, **kwargs):
        return {"domains": len(domains), "indexed_pages": 0, "sites": 0, "accepted": 0,
                "errors": {}, "reports": {}}

    monkeypatch.setattr(FrontierAgent, "next_batch", _fresh_batch)
    monkeypatch.setattr(Orchestrator, "run_domains", _run_domains)

    report = await pipeline.run_discovery_pass(
        store=store,
        settings=EngineSettings(discovery_batch_size=1, discovery_batch_pause_seconds=0.0,
                                worker_idle_backoff_seconds=1, discovery_max_batches_per_pass=3),
    )

    assert report["batches"] == 3


def test_the_idle_backoff_is_at_least_fifteen_seconds() -> None:
    from app.indexer import scheduler

    assert scheduler._idle_seconds(EngineSettings(worker_idle_backoff_seconds=1)) == 15.0
    assert scheduler._idle_seconds(EngineSettings(worker_idle_backoff_seconds=45)) == 45.0


async def test_a_thin_queue_is_refilled_by_the_scheduler(temp_db, monkeypatch, caplog) -> None:
    from app.indexer import scheduler

    store = temp_db

    async def _fake_refill(*, target=None, store=None, settings=None):
        return {"before": 0, "after": 150, "seeds": 150, "sitemap": 0, "links": 0, "target": 150}

    monkeypatch.setattr("app.indexer.crawler.refill_frontier", _fake_refill, raising=False)

    with caplog.at_level("INFO"):
        await scheduler._refill_if_low(EngineSettings(frontier_low_water=20))

    messages = [record.getMessage() for record in caplog.records]
    assert any("Frontier refill completed: added 150 URLs to crawl_queue. Depth is now 150" in m
               for m in messages), messages


async def test_a_healthy_queue_is_left_alone(temp_db, monkeypatch) -> None:
    from app.indexer import scheduler

    store = temp_db
    for i in range(25):
        await store.queue_url(f"https://site{i}.gov.in/", domain=f"site{i}.gov.in")

    called = {"refill": False}

    async def _fake_refill(*args, **kwargs):
        called["refill"] = True
        return {}

    monkeypatch.setattr("app.indexer.crawler.refill_frontier", _fake_refill, raising=False)
    await scheduler._refill_if_low(EngineSettings(frontier_low_water=20))

    assert called["refill"] is False


async def test_a_queued_url_is_consumable_by_the_discovery_pass(temp_db, monkeypatch) -> None:
    """A full queue must feed the pipeline, not just sit there."""
    from app.indexer import crawler as pipeline

    store = temp_db
    await store.ingest_documents(
        [{"url": "https://eci.gov.in/", "canonical_url": "https://eci.gov.in/",
          "title": "Election Commission", "description": "Elections.", "payload_bytes": 90}]
    )

    async def _fake_harvest(self, host, *, fetcher=None):
        return [f"https://{host}/p{i}" for i in range(15)]

    monkeypatch.setattr(FrontierAgent, "harvest", _fake_harvest)
    monkeypatch.setattr(pipeline, "seeds_for", lambda categories=None: [])

    await pipeline.refill_frontier(
        target=15, store=store,
        settings=EngineSettings(harvest_hosts_per_pass=1, frontier_low_water=20),
    )

    frontier = FrontierAgent(store=store, config=FrontierConfig())
    hosts = await frontier.next_batch(5)
    assert hosts, "the queued URLs must yield crawlable hosts"
    assert hosts[0] == "eci.gov.in"


async def test_pending_queue_hosts_returns_distinct_domains(temp_db) -> None:
    store = temp_db
    await store.queue_url("https://a.gov.in/one", domain="a.gov.in")
    await store.queue_url("https://a.gov.in/two", domain="a.gov.in")
    await store.queue_url("https://b.gov.in/one", domain="b.gov.in")

    hosts = await store.pending_queue_hosts(10)
    assert hosts == ["a.gov.in", "b.gov.in"]
