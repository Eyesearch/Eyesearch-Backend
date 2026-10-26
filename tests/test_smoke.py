"""Offline smoke tests: contracts, cache path, degradation, formatter rules."""

from __future__ import annotations

import asyncio

import pytest

from app.services import formatter, live_retrieval
from app.services.live_retrieval import RawResult, RetrievalOutcome


def _fake_outcome(query: str, limit: int) -> RetrievalOutcome:
    return RetrievalOutcome(
        results=[
            RawResult(
                title=f"Result one for {query}",
                url="https://en.wikipedia.org/wiki/Espresso?utm_source=test",
                snippet="Espresso is a concentrated form of coffee. It is brewed by forcing hot water.",
                source="duckduckgo",
                position=0,
            ),
            RawResult(
                title="Espresso — video guide",
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                snippet="A short walkthrough of home espresso machines.",
                source="duckduckgo_html",
                position=0,
            ),
            RawResult(
                title="Duplicate of result one",
                url="https://en.wikipedia.org/wiki/Espresso",
                snippet="duplicate",
                source="duckduckgo_lite",
                position=0,
            ),
        ],
        providers={"duckduckgo": 1, "duckduckgo_html": 1},
    )


@pytest.fixture
def live_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _stub(query: str, limit: int, **_: object) -> RetrievalOutcome:
        return _fake_outcome(query, limit)

    monkeypatch.setattr(live_retrieval, "retrieve", _stub)


async def test_health(client) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["db"] == "up"
    assert "providers" in body

    alias = await client.get("/healthz")
    assert alias.status_code == 200


async def test_search_then_cache(client, live_stub) -> None:
    first = await client.get("/v1/search", params={"q": "unique query alpha", "limit": 5})
    assert first.status_code == 200
    body = first.json()
    assert body["degraded"] is False
    assert body["cache_hit"] is False
    assert 1 <= len(body["results"]) <= 5
    # deduplicated: the third raw result shares a canonical URL with the first
    assert len(body["results"]) == 2
    card = body["results"][0]
    assert set(card) >= {"id", "title", "url", "snippet", "source", "poster_url", "thumbnail_url", "score"}
    youtube = body["results"][1]
    assert youtube["kind"] == "video"
    assert youtube["thumbnail_url"].startswith("https://img.youtube.com/vi/")

    await asyncio.sleep(0.4)  # let the background cache write land
    second = await client.get("/v1/search", params={"q": "unique query alpha", "limit": 5})
    assert second.status_code == 200
    assert second.json()["cache_hit"] is True


async def test_retrieve_markdown_and_json(client, live_stub) -> None:
    md = await client.post(
        "/v1/retrieve", json={"query": "markdown contract", "format": "markdown", "limit": 3}
    )
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    text = md.text
    assert text.startswith("# markdown contract")
    assert "https://en.wikipedia.org/wiki/Espresso" in text
    assert "utm_source" not in text  # tracking params survive nowhere

    js = await client.post(
        "/v1/retrieve", json={"query": "json contract", "format": "json", "limit": 3}
    )
    assert js.status_code == 200
    payload = js.json()
    assert payload["format"] == "json"
    assert payload["results"][0]["title"]
    assert "poster_url" not in payload["results"][0]  # AI payload has no presentation fields

    get_md = await client.get("/v1/retrieve", params={"q": "get form"})
    assert get_md.status_code == 200
    assert get_md.headers["content-type"].startswith("text/markdown")


async def test_degraded_response(client, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _stub(query: str, limit: int, **_: object) -> RetrievalOutcome:
        return RetrievalOutcome(
            results=[
                RawResult(
                    title="Only result",
                    url="https://example.org/only",
                    snippet="The chain lost two providers.",
                    source="duckduckgo",
                )
            ],
            failed=[
                {"provider": "duckduckgo_html", "error": "timeout"},
                {"provider": "duckduckgo_lite", "error": "circuit_open"},
            ],
            providers={"duckduckgo": 1},
        )

    monkeypatch.setattr(live_retrieval, "retrieve", _stub)
    response = await client.get("/v1/search", params={"q": "degraded path beta"})
    body = response.json()
    assert body["degraded"] is True
    assert len(body["results"]) == 1

    md = await client.get("/v1/retrieve", params={"q": "degraded path gamma"})
    assert "> Partial result: 2 providers unavailable." in md.text


async def test_empty_chain_is_not_a_500(client, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _stub(query: str, limit: int, **_: object) -> RetrievalOutcome:
        return RetrievalOutcome(
            results=[],
            failed=[
                {"provider": "duckduckgo", "error": "timeout"},
                {"provider": "duckduckgo_html", "error": "timeout"},
                {"provider": "duckduckgo_lite", "error": "timeout"},
            ],
        )

    monkeypatch.setattr(live_retrieval, "retrieve", _stub)
    response = await client.get("/v1/search", params={"q": "nothing here delta"})
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert response.json()["degraded"] is True


async def test_bad_request_envelope(client) -> None:
    blank = await client.get("/v1/search", params={"q": "   "})
    assert blank.status_code == 400
    assert blank.json()["error"]["code"] == "bad_request"
    assert blank.json()["error"]["retryable"] is False

    too_big = await client.get("/v1/search", params={"q": "x", "limit": 500})
    assert too_big.status_code == 400
    assert too_big.json()["error"]["code"] == "bad_request"


def test_formatter_rules() -> None:
    assert formatter.canonical_url("https://www.Example.com/path/?utm_source=a&b=2") == (
        "https://example.com/path?b=2"
    )
    assert formatter.canonical_url("//duckduckgo.com/x") == "https://duckduckgo.com/x"
    trimmed = formatter.clean_snippet("A sentence here. " + "word " * 100, 60)
    assert trimmed.endswith(".") or trimmed.endswith("…")
    assert len(trimmed) <= 61
    poster, thumb = formatter.derive_media("https://youtu.be/dQw4w9WgXcQ")
    assert poster and thumb and "dQw4w9WgXcQ" in poster
    assert formatter.derive_media("https://example.com") == (None, None)
    assert formatter.classify_kind("https://www.imdb.com/title/tt0083658/") == "film"


def test_breaker_opens_and_half_opens() -> None:
    breaker = live_retrieval.CircuitBreaker(threshold=2, reset_after_s=60.0)
    assert breaker.is_open("duckduckgo") is False
    breaker.record_failure("duckduckgo")
    assert breaker.is_open("duckduckgo") is False
    breaker.record_failure("duckduckgo")
    assert breaker.is_open("duckduckgo") is True
    breaker.record_success("duckduckgo")
    assert breaker.is_open("duckduckgo") is False


def test_bing_url_decoding() -> None:
    import base64

    target = "https://www.isro.gov.in/Chandrayaan3.html"
    token = "a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    wrapped = f"https://www.bing.com/ck/a?!&&p=abc&u={token}&ntb=1"
    assert live_retrieval._decode_bing_url(wrapped) == target
    # a plain link is passed through untouched
    assert live_retrieval._decode_bing_url("https://example.com/x") == "https://example.com/x"


def test_provider_chain_gating() -> None:
    from app.config import Settings

    default = Settings()
    assert default.enable_bing_scrape is False
    assert "bing" not in live_retrieval.provider_chain(default)
    assert live_retrieval.provider_chain(default)[0] == "duckduckgo"
    assert "bing" in live_retrieval.provider_chain(Settings(ENABLE_BING_SCRAPE="true"))
