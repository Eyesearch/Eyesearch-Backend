"""Timeouts, politeness and the circuit breaker — the throughput levers."""

from __future__ import annotations

import asyncio

import pytest

from app.agents import load_agent
from app.crawler.breaker import DomainCircuitBreaker
from app.engine.config import Settings

FetcherAgent = load_agent("02_fetcher_agent").FetcherAgent


def settings(**overrides) -> Settings:
    base = dict(environment="test")
    base.update(overrides)
    return Settings(_env_file=None, **base)


# ---------------------------------------------------------------------------
# The HTTP client
# ---------------------------------------------------------------------------


def test_the_phases_have_their_own_windows():
    """A scalar applies to every phase, so `7` meant seven seconds to connect
    *and* seven to read: twice the number anyone wrote down."""
    fetcher = FetcherAgent(settings())
    timeout = fetcher.client.timeout

    assert timeout.connect == 3.0
    assert timeout.read == 5.0
    assert timeout.write == 3.0
    assert timeout.pool == 3.0


def test_the_pool_is_bounded():
    """Unbounded, sixty dead hosts open sixty sockets and leave them in TIME_WAIT."""
    fetcher = FetcherAgent(settings())

    assert fetcher.client._transport._pool._max_connections == 20
    assert fetcher.client._transport._pool._max_keepalive_connections == 5


def test_the_pool_bounds_are_configurable():
    fetcher = FetcherAgent(settings(crawl_max_connections=4, crawl_max_keepalive_connections=2))

    assert fetcher.client._transport._pool._max_connections == 4


# ---------------------------------------------------------------------------
# Politeness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_same_domain_is_spaced():
    fetcher = FetcherAgent(settings(crawl_domain_politeness_seconds=0.15))

    started = asyncio.get_running_loop().time()
    await fetcher._respect_politeness("https://a.in/1")
    await fetcher._respect_politeness("https://a.in/2")
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed >= 0.12, elapsed


@pytest.mark.asyncio
async def test_different_domains_are_not_spaced():
    fetcher = FetcherAgent(settings(crawl_domain_politeness_seconds=1.0))

    started = asyncio.get_running_loop().time()
    await fetcher._respect_politeness("https://a.in/1")
    await fetcher._respect_politeness("https://b.in/1")
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.5, elapsed


@pytest.mark.asyncio
async def test_politeness_can_be_switched_off():
    fetcher = FetcherAgent(settings(crawl_domain_politeness_seconds=0))

    started = asyncio.get_running_loop().time()
    for _ in range(5):
        await fetcher._respect_politeness("https://a.in/x")
    assert asyncio.get_running_loop().time() - started < 0.5


# ---------------------------------------------------------------------------
# The breaker
# ---------------------------------------------------------------------------


def test_a_domain_opens_after_its_threshold():
    breaker = DomainCircuitBreaker(threshold=3, cooldown=100)

    assert breaker.record_failure("dead.in", now=1000.0) is False
    assert breaker.record_failure("dead.in", now=1001.0) is False
    assert breaker.record_failure("dead.in", now=1002.0) is True
    assert breaker.is_open("dead.in", now=1003.0) is True


def test_one_success_closes_it():
    breaker = DomainCircuitBreaker(threshold=2, cooldown=100)
    breaker.record_failure("x.in", now=1.0)
    breaker.record_failure("x.in", now=2.0)

    breaker.record_success("x.in")

    assert breaker.is_open("x.in", now=3.0) is False


def test_it_reopens_on_its_own_after_the_cooldown():
    """A host that comes back is crawled again without a restart."""
    breaker = DomainCircuitBreaker(threshold=1, cooldown=100)
    breaker.record_failure("x.in", now=1000.0)

    assert breaker.is_open("x.in", now=1050.0) is True
    assert breaker.is_open("x.in", now=1101.0) is False


def test_an_unseen_domain_is_closed():
    assert DomainCircuitBreaker().is_open("never-seen.in") is False


def test_a_domain_is_reported_once():
    breaker = DomainCircuitBreaker(threshold=1, cooldown=100)
    breaker.record_failure("x.in", now=1.0)

    assert breaker.should_report("x.in") is True
    assert breaker.should_report("x.in") is False


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_the_orchestrator_owns_a_breaker():
    import inspect

    from app.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator)

    assert "DomainCircuitBreaker" in source
    assert "circuit_open" in source


def test_a_404_does_not_open_a_domain():
    """A 404 is the server answering; the page is gone, the host is fine."""
    import inspect

    from app.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator.run_batch)
    before_breaker = source.split("record_failure")[0]

    # Only transport failures are counted against the domain.
    assert '"timeout", "connect_error", "connection_error"' in before_breaker
    assert "http_status:4" not in before_breaker
