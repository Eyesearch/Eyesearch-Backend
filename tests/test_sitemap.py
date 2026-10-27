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
