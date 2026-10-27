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
