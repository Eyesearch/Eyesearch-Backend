"""The six-agent architecture: interfaces, isolation and the orchestrator."""

from __future__ import annotations

import importlib

import pytest

from app import database
from app.agents import (
    AGENTS,
    FetcherAgent,
    FrontierAgent,
    IndexerAgent,
    ParserAgent,
    QualityAgent,
    SemanticAIAgent,
)
from app.agents import FrontierConfig
from app.config import Settings
from app.engine.config import EngineSettings
from app.engine.store import get_store
from app.orchestrator import Orchestrator, OrchestratorReport

GOOD_HTML = (
    "<html><head><title>Karnataka services portal</title>"
    "<meta name='description' content='Official services portal for Karnataka.'></head>"
    "<body><h1>Services</h1><h2>Departments</h2>"
    "<nav><a href='/about'>About</a><a href='/contact'>Contact</a></nav>"
    "<p>" + ("The portal lists citizen services and departmental notices. " * 8) + "</p>"
    "</body></html>"
)

THIN_HTML = (
    "<html><head><title>Empty portal</title>"
    "<meta name='description' content='A portal with no rendered text at all.'></head>"
    "<body><div id='app'></div></body></html>"
)


@pytest.fixture()
async def temp_db(tmp_path):
    settings = Settings(turso_db_url=f"file:{tmp_path}/agents.db")
    await database.connect(settings)
    await database.initialize_schema()
    yield
    await database.close()


# --- structure -----------------------------------------------------------

def test_every_agent_directory_exposes_an_async_entry_point() -> None:
    folders = {
        "frontier": "01_frontier_agent",
        "fetcher": "02_fetcher_agent",
        "parser": "03_parser_agent",
        "quality": "04_quality_agent",
        "indexer": "05_indexer_agent",
        "semantic": "06_semantic_ai_agent",
    }
    assert tuple(folders) == AGENTS

    for name, folder in folders.items():
        module = importlib.import_module(f"app.agents.{folder}.agent")
        classes = [
            value
            for value in vars(module).values()
            if isinstance(value, type) and value.__module__ == module.__name__
            and value.__name__.endswith("Agent")
        ]
        assert classes, f"{folder} exposes no agent class"
        agent = classes[0]()
        assert agent.name == name
        # the standard async entry point exists on every agent
        entry = getattr(agent, "fetch", None) or getattr(agent, "parse", None) or getattr(
            agent, "check", None
        ) or getattr(agent, "persist", None) or getattr(agent, "answer", None) or getattr(
            agent, "next_batch", None
        )
        assert entry is not None, f"{name} has no async entry point"


def test_the_agent_modules_also_ship_their_support_file() -> None:
    expected = {
        "01_frontier_agent": "config",
        "02_fetcher_agent": "headers",
        "03_parser_agent": "extractors",
        "04_quality_agent": "validator",
        "05_indexer_agent": "storage",
        "06_semantic_ai_agent": "prompts",
    }
    for folder, support in expected.items():
        module = importlib.import_module(f"app.agents.{folder}.{support}")
        assert module is not None


# --- individual agents ---------------------------------------------------

def test_frontier_prioritises_authoritative_domains() -> None:
    config = FrontierConfig()
    assert config.is_authoritative("eci.gov.in")
    assert config.is_authoritative("ssc.nic.in")
    assert not config.is_authoritative("example.com")
    assert config.is_excluded("facebook.com")
    assert config.is_excluded("cdn.google-analytics.com")


async def test_frontier_queues_and_reads_back(temp_db) -> None:
    agent = FrontierAgent(store=get_store(), config=FrontierConfig(batch_size=3))
    assert await agent.enqueue("eci.gov.in") is True
    assert await agent.enqueue("facebook.com") is False       # excluded
    assert await agent.enqueue("eci.gov.in") is False          # already queued

    batch = await agent.next_batch()
    assert batch == ["eci.gov.in"]
    assert await agent.pending() == 1


async def test_parser_caps_the_body_at_1200_characters() -> None:
    agent = ParserAgent(EngineSettings())
    long_html = GOOD_HTML.replace(
        "The portal lists citizen services and departmental notices. " * 8,
        "The portal lists citizen services and departmental notices. " * 80,
    )
    result = await agent.parse("https://karnataka.gov.in/", long_html)

    assert result.ok
    assert len(result.document.body_text) <= 1200
    assert len(result.document.body_text) >= 1100
    assert result.document.headings.startswith("Services")


async def test_parser_reports_a_reason_instead_of_raising() -> None:
    agent = ParserAgent(EngineSettings())
    assert (await agent.parse("https://x.gov.in/", None)).reason == "no_html"
    thin = await agent.parse("https://x.gov.in/", THIN_HTML)
    assert thin.ok is False
    assert thin.reason == "low_text_density"


async def test_quality_rejects_thin_and_duplicate_documents() -> None:
    parser = ParserAgent(EngineSettings())
    quality = QualityAgent()

    good = await parser.parse("https://karnataka.gov.in/", GOOD_HTML)
    assert (await quality.check(good.document)).ok is True
    # the same page twice is a duplicate within one run
    assert (await quality.check(good.document)).reason == "duplicate"

    thin = await parser.parse("https://x.gov.in/", THIN_HTML)
    assert thin.ok is False


async def test_indexer_writes_and_reports_the_running_total(temp_db) -> None:
    parser = ParserAgent(EngineSettings())
    indexer = IndexerAgent(store=get_store(), settings=EngineSettings())
    document = (await parser.parse("https://karnataka.gov.in/", GOOD_HTML)).document

    outcome = await indexer.persist([document])

    assert outcome["pages"] >= 1
    assert await indexer.count() >= 1
    assert await indexer.can_write() is True


async def test_semantic_agent_answers_from_context_without_a_model() -> None:
    agent = SemanticAIAgent()
    assert agent.enabled is False

    result = await agent.answer(
        "election commission",
        [{"title": "Election Commission of India", "url": "https://eci.gov.in/",
          "snippet": "Conducts elections."}],
    )
    assert "Election Commission of India" in result.text
    assert result.mode == "grounded"

    chunks = [chunk async for chunk in agent.stream("election commission", [])]
    assert chunks[-1]["done"] is True
    assert "".join(c.get("delta", "") for c in chunks)


# --- the orchestrator ----------------------------------------------------

async def test_the_orchestrator_pipes_a_page_through_every_stage(temp_db, monkeypatch) -> None:
    parser = ParserAgent(EngineSettings())

    async def _fake_fetch(url: str):
        from app.agents import FetchResult

        return FetchResult(url=url, html=GOOD_HTML, status=200)

    orchestrator = Orchestrator(store=get_store(), settings=EngineSettings())
    monkeypatch.setattr(orchestrator.fetcher, "fetch", _fake_fetch)

    report = await orchestrator.run(["karnataka.gov.in"])

    assert report["attempted"] == 1
    assert report["fetched"] == 1
    assert report["parsed"] == 1
    assert report["accepted"] == 1
    assert report["indexed_pages"] >= 1
    assert await get_store().count_pages() >= 1
    assert parser is not None


async def test_a_fetch_failure_does_not_break_the_run(temp_db, monkeypatch) -> None:
    from app.agents import FetchResult

    calls = {"n": 0}

    async def _flaky(url: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection reset")
        return FetchResult(url=url, html=GOOD_HTML, status=200)

    orchestrator = Orchestrator(store=get_store(), settings=EngineSettings())
    monkeypatch.setattr(orchestrator.fetcher, "fetch", _flaky)

    report = await orchestrator.run(["broken.gov.in", "karnataka.gov.in"])

    # one stage failed, the other page still landed
    assert report["errors"], report
    assert report["indexed_pages"] >= 1


async def test_a_parser_failure_is_isolated(temp_db, monkeypatch) -> None:
    from app.agents import FetchResult

    async def _fake_fetch(url: str):
        return FetchResult(url=url, html=THIN_HTML, status=200)

    orchestrator = Orchestrator(store=get_store(), settings=EngineSettings())
    monkeypatch.setattr(orchestrator.fetcher, "fetch", _fake_fetch)

    report = await orchestrator.run(["thin.gov.in"])

    assert report["rejected"] == 1
    assert report["indexed_pages"] == 0
    assert any(key.startswith("parse") or key.startswith("quality") for key in report["errors"])


async def test_the_orchestrator_stops_at_the_storage_ceiling(temp_db, monkeypatch) -> None:
    async def _never(url: str):  # pragma: no cover - paused before any fetch
        raise AssertionError("paused")

    orchestrator = Orchestrator(store=get_store(), settings=EngineSettings(storage_page_limit=0))
    monkeypatch.setattr(orchestrator.fetcher, "fetch", _never)

    report = await orchestrator.run(["karnataka.gov.in"])
    assert report.get("paused") is True


def test_the_report_counts_stage_failures() -> None:
    report = OrchestratorReport()
    report.fail("fetch")
    report.fail("fetch")
    report.fail("parse")
    assert report.as_dict()["errors"] == {"fetch": 2, "parse": 1}


# --- the loops route through the orchestrator -----------------------------

async def test_run_seed_pass_goes_through_the_frontier_and_orchestrator(temp_db, monkeypatch) -> None:
    from app.indexer import crawler as pipeline
    from app.engine.text import ExtractedDocument

    enqueued: list[str] = []
    ran: dict[str, list] = {}

    async def _fake_enqueue(self, host, *, reason="discovered"):
        enqueued.append(host)
        return True

    async def _fake_run_domains(self, domains, **kwargs):
        ran["domains"] = list(domains)
        return {
            "domains": len(domains),
            "indexed_pages": 2,
            "sites": 1,
            "accepted": 2,
            "errors": {},
            "reports": {
                domains[0]: {"category": "government", "fetched": 1, "extracted": 2,
                             "stored": 2, "skipped": 0, "reasons": {}},
            },
        }

    from app.agents import FrontierAgent

    monkeypatch.setattr(FrontierAgent, "enqueue", _fake_enqueue)
    from app.orchestrator import Orchestrator

    monkeypatch.setattr(Orchestrator, "run_domains", _fake_run_domains)

    report = await pipeline.run_seed_pass(
        seeds=[pipeline.CategorySeed("https://eci.gov.in", "Election Commission", "government")],
        settings=EngineSettings(seed_crawl_enabled=True),
        store=get_store(),
    )

    assert enqueued == ["eci.gov.in"], "the seed must enter the frontier"
    assert ran["domains"] == ["eci.gov.in"], "and then flow through the orchestrator"
    assert report["stored"] == 2
    assert report["sitelinks"] == 1
    assert "Election Commission" in report["categories"]


async def test_run_discovery_pass_pulls_batches_from_the_frontier(temp_db, monkeypatch) -> None:
    from app.indexer import crawler as pipeline
    from app.orchestrator import Orchestrator

    store = get_store()
    for host in ("maharashtra.gov.in", "tn.gov.in", "up.gov.in"):
        await store.enqueue_domain(host)

    calls: list[list[str]] = []

    async def _fake_run_domains(self, domains, **kwargs):
        calls.append(list(domains))
        return {"domains": len(domains), "indexed_pages": len(domains), "sites": len(domains),
                "accepted": len(domains), "errors": {}, "reports": {}}

    monkeypatch.setattr(Orchestrator, "run_domains", _fake_run_domains)

    report = await pipeline.run_discovery_pass(
        store=store, settings=EngineSettings(seed_crawl_enabled=True, discovery_batch_size=2,
                                             discovery_batch_pause_seconds=0.0)
    )

    assert report["batches"] == 2            # two, then one
    assert report["attempted"] == 3
    assert [len(batch) for batch in calls] == [2, 1]
    assert await store.pending_domain_count() == 0


# --- bot-block resilience -------------------------------------------------

def test_the_fetcher_rotates_realistic_browser_headers() -> None:
    import importlib

    headers_module = importlib.import_module("app.agents.02_fetcher_agent.headers")

    chrome = headers_module.headers_for(0)
    safari = headers_module.headers_for(1)

    assert "Chrome/128" in chrome["User-Agent"]
    assert chrome["Accept-Language"] == "en-US,en;q=0.9"
    assert '"Google Chrome";v="128"' in chrome["Sec-Ch-Ua"]
    assert chrome["Sec-Ch-Ua-Platform"] == '"Windows"'
    assert "Safari/605" in safari["User-Agent"]
    assert chrome["User-Agent"] != safari["User-Agent"], "attempts must not repeat a fingerprint"
    assert headers_module.next_profile()["User-Agent"].startswith("Mozilla/5.0")


def test_a_blocked_status_marks_the_host_restricted() -> None:
    import importlib

    restrictions = importlib.import_module("app.agents.02_fetcher_agent.restrictions")
    restrictions.clear()

    assert restrictions.is_restricting(403) is True
    assert restrictions.is_restricting(202) is True
    assert restrictions.is_restricting(429) is True
    assert restrictions.is_restricting(200) is False
    assert restrictions.is_restricting(404) is False

    restrictions.mark_restricted("blocked.gov.in", 403)
    assert restrictions.is_restricted("blocked.gov.in") is True
    assert restrictions.is_restricted("www.blocked.gov.in") is True
    assert restrictions.is_restricted("open.gov.in") is False
    # a zero-length cooldown expires immediately
    assert restrictions.is_restricted("blocked.gov.in", cooldown_seconds=0) is False
    restrictions.clear()


async def test_a_403_is_bypassed_immediately_without_a_second_request() -> None:
    import importlib

    restrictions = importlib.import_module("app.agents.02_fetcher_agent.restrictions")
    restrictions.clear()

    calls: list[str] = []

    async def _blocked(client, url, settings, **kwargs):
        calls.append(url)
        return None, 403

    agent = FetcherAgent(EngineSettings(), fetch_fn=_blocked)

    first = await agent.fetch("https://blocked.gov.in/")
    assert first.ok is False
    assert first.restricted is True
    assert first.attempts == 1

    # the second call never reaches the network: the host is restricted
    second = await agent.fetch("https://blocked.gov.in/notices")
    assert second.restricted is True
    assert second.attempts == 0
    assert calls == ["https://blocked.gov.in/"]
    restrictions.clear()


async def test_a_202_challenge_is_treated_as_a_block() -> None:
    import importlib

    restrictions = importlib.import_module("app.agents.02_fetcher_agent.restrictions")
    restrictions.clear()

    async def _challenge(client, url, settings, **kwargs):
        return None, 202

    agent = FetcherAgent(EngineSettings(), fetch_fn=_challenge)
    result = await agent.fetch("https://challenged.gov.in/")

    assert result.restricted is True
    assert restrictions.is_restricted("challenged.gov.in") is True
    restrictions.clear()


async def test_the_backlog_drops_a_query_after_repeated_blocks(temp_db, monkeypatch) -> None:
    from app.indexer import backlog_worker

    store = get_store()
    await store.enqueue_backlog("a query that stays blocked", hits=1)

    async def _nothing(query, *, store=None, settings=None, client=None):
        return {"urls": 0, "inserted": 0, "skipped": 0, "reason": "unreachable"}

    monkeypatch.setattr(backlog_worker, "resolve_query", _nothing)
    settings = EngineSettings(crawl_enabled=True, backlog_max_attempts=2)

    first = await backlog_worker.run_backlog_once(store=store, settings=settings)
    assert first["dropped"] == 0
    assert await store.pending_backlog(5) == ["a query that stays blocked"]

    second = await backlog_worker.run_backlog_once(store=store, settings=settings)
    assert second["dropped"] == 1
    assert await store.pending_backlog(5) == [], "an exhausted query goes to history"


async def test_a_blocked_query_is_dropped_on_the_first_pass(temp_db, monkeypatch) -> None:
    """'zepto' returning 403 everywhere must not sit in the queue for three passes."""
    from app.indexer import backlog_worker

    store = get_store()
    await store.enqueue_backlog("zepto", hits=2)

    async def _blocked(query, *, store=None, settings=None, client=None):
        return {"urls": 3, "inserted": 0, "skipped": 3, "reason": "blocked"}

    monkeypatch.setattr(backlog_worker, "resolve_query", _blocked)

    report = await backlog_worker.run_backlog_once(
        store=store, settings=EngineSettings(crawl_enabled=True, backlog_max_attempts=3)
    )

    assert report["dropped"] == 1
    assert await store.pending_backlog(5) == []


# --- queue refill and blocked-query purge ---------------------------------

async def test_the_frontier_mirrors_queued_domains_into_crawl_queue(temp_db) -> None:
    frontier = FrontierAgent(store=get_store(), config=FrontierConfig())

    assert await frontier.enqueue("eci.gov.in", reason="seed") is True
    assert await frontier.queue_depth() == 1

    result = await database.execute_safe("SELECT url, status FROM crawl_queue")
    queued = [(str(url), str(status)) for url, status in result.rows]
    assert queued == [("https://eci.gov.in/", "pending")]

    await frontier.mark_done("eci.gov.in")
    assert await frontier.queue_depth() == 0


async def test_the_refill_pass_fills_the_buffer_to_the_target(temp_db) -> None:
    from app.indexer import crawler as pipeline

    store = get_store()
    report = await pipeline.refill_frontier(
        target=25, store=store, settings=EngineSettings(frontier_buffer_target=25)
    )

    assert report["after"] == 25
    assert report["seeds"] == 25
    assert await store.queue_depth() == 25
    # the backlog the pipeline reads is populated too
    assert await store.pending_domain_count() == 25


async def test_the_refill_pass_does_not_overfill(temp_db) -> None:
    from app.indexer import crawler as pipeline

    store = get_store()
    await pipeline.refill_frontier(target=10, store=store, settings=EngineSettings())
    again = await pipeline.refill_frontier(target=10, store=store, settings=EngineSettings())

    assert again["before"] == 10
    assert again["after"] == 10
    assert again["seeds"] == 0, "a full buffer queues nothing new"


async def test_blocked_backlog_rows_are_purged(temp_db) -> None:
    store = get_store()
    client = database.get_client()

    await store.enqueue_backlog("zepto", hits=3)
    await store.enqueue_backlog("blinkit", hits=2)
    await store.enqueue_backlog("karnataka budget", hits=1)
    await client.execute("UPDATE crawl_backlog SET last_error = 'blocked (403)' WHERE query IN ('zepto','blinkit')")

    purged = await store.purge_blocked_queries()

    assert purged >= 1
    pending = await store.pending_backlog(10)
    assert "zepto" not in pending
    assert "blinkit" not in pending
    assert "karnataka budget" in pending
