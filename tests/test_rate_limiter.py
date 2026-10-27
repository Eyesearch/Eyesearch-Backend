"""The process-wide database throttle, and the client that uses it.

The invariant is a ceiling, not an average: two Turso requests per second for
the whole process, no matter how many workers are running.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app.engine.store import IndexStore, PageDocument

from app.libsql_http import (
    DatabaseUnavailable,
    LibsqlError,
    ProcessRateLimiter,
    TursoHttpClient,
    encode_arg,
    rewrite_scheme,
)


async def test_limiter_enforces_the_interval_between_calls():
    limiter = ProcessRateLimiter(interval_seconds=0.05)

    started = time.monotonic()
    for _ in range(3):
        await limiter.acquire()
    elapsed = time.monotonic() - started

    # Two gaps of 0.05s; allow scheduling slop but not a free pass.
    assert elapsed >= 0.08
    assert limiter.calls == 3


async def test_limiter_serialises_concurrent_callers():
    limiter = ProcessRateLimiter(interval_seconds=0.05)
    started = time.monotonic()

    await asyncio.gather(*(limiter.acquire() for _ in range(4)))
    elapsed = time.monotonic() - started

    assert elapsed >= 0.13
    assert limiter.calls == 4


async def test_limiter_default_is_two_requests_per_second():
    limiter = ProcessRateLimiter()
    assert limiter.interval_seconds == 0.5


def test_limiter_rejects_a_negative_interval():
    with pytest.raises(ValueError):
        ProcessRateLimiter(interval_seconds=-1)


def test_libsql_scheme_is_rewritten_to_https():
    assert rewrite_scheme("libsql://db.turso.io") == "https://db.turso.io"
    assert rewrite_scheme("wss://db.turso.io") == "https://db.turso.io"
    assert rewrite_scheme("ws://db.turso.io") == "http://db.turso.io"
    assert rewrite_scheme("https://db.turso.io") == "https://db.turso.io"


def test_arguments_are_encoded_as_typed_hrana_values():
    """integer is a decimal string; float is a JSON number. Not interchangeable."""
    assert encode_arg(None) == {"type": "null"}
    assert encode_arg(7) == {"type": "integer", "value": "7"}
    assert encode_arg(1.5) == {"type": "float", "value": 1.5}
    assert encode_arg("x") == {"type": "text", "value": "x"}


def test_a_float_is_never_encoded_as_a_string():
    """Turso rejects `"0.999"` for an f64 with HTTP 400, so this must not regress."""
    encoded = encode_arg(0.999)

    assert encoded == {"type": "float", "value": 0.999}
    assert not isinstance(encoded["value"], str)


def test_a_decimal_is_encoded_as_a_float():
    from decimal import Decimal

    assert encode_arg(Decimal("0.999")) == {"type": "float", "value": 0.999}


def _pipeline_response(rows: list[int] | None = None) -> dict:
    return {
        "results": [
            {
                "type": "ok",
                "response": {
                    "type": "execute",
                    "result": {
                        "cols": [{"name": "n"}],
                        "rows": [[{"type": "integer", "value": str(value)}] for value in rows or []],
                        "affected_row_count": len(rows or []),
                    },
                },
            }
        ]
    }


async def test_client_posts_to_the_pipeline_endpoint_with_a_bearer_token():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_pipeline_response([3]))

    client = TursoHttpClient(
        "libsql://db.turso.io",
        "secret-token",
        limiter=ProcessRateLimiter(0.0),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.execute("SELECT COUNT(*) FROM pages")
    finally:
        await client.close()

    assert seen["url"] == "https://db.turso.io/v2/pipeline"
    assert seen["auth"] == "Bearer secret-token"
    assert result.scalar() == 3


async def test_client_raises_when_a_step_failed_despite_http_200():
    """Turso answers 200 with a per-step error; that must not read as success."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "type": "error",
                        "error": {
                            "message": "SQLITE_CONSTRAINT: UNIQUE constraint failed: sites.domain"
                        },
                    }
                ]
            },
        )

    client = TursoHttpClient(
        "libsql://db.turso.io",
        "token",
        limiter=ProcessRateLimiter(0.0),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(LibsqlError) as raised:
            await client.execute("INSERT INTO sites (domain) VALUES (?)", ("x",))
    finally:
        await client.close()

    message = str(raised.value)
    assert "UNIQUE constraint failed" in message
    assert "INSERT INTO sites" in message


async def test_client_reports_an_unreachable_database_as_unavailable():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = TursoHttpClient(
        "libsql://db.turso.io",
        "token",
        limiter=ProcessRateLimiter(0.0),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DatabaseUnavailable):
            await client.execute("SELECT 1")
    finally:
        await client.close()


async def test_client_goes_through_the_limiter():
    limiter = ProcessRateLimiter(interval_seconds=0.05)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_pipeline_response([1]))

    client = TursoHttpClient(
        "libsql://db.turso.io",
        "token",
        limiter=limiter,
        transport=httpx.MockTransport(handler),
    )
    started = time.monotonic()
    try:
        await client.execute("SELECT 1")
        await client.execute("SELECT 1")
        await client.execute("SELECT 1")
    finally:
        await client.close()

    assert limiter.calls == 3
    assert time.monotonic() - started >= 0.08


async def test_a_page_write_sends_quality_score_as_a_json_number():
    """The whole point: the bytes on the wire, not just `encode_arg`.

    `quality_score` is a REAL column. If it reaches Turso as the string
    "0.999" the pipeline is rejected with HTTP 400 and no page is ever
    stored, so this asserts the serialised request body.
    """
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        count = len(captured["body"]["requests"])
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "type": "ok",
                        "response": {
                            "type": "execute",
                            "result": {
                                "cols": [{"name": "n"}],
                                "rows": [[{"type": "integer", "value": "1"}]],
                                "affected_row_count": 1,
                            },
                        },
                    }
                    for _ in range(count)
                ]
            },
        )

    client = TursoHttpClient(
        "libsql://db.turso.io",
        "token",
        limiter=ProcessRateLimiter(0.0),
        transport=httpx.MockTransport(handler),
    )
    try:
        store = IndexStore(client)
        await store.persist_batch(
            [
                PageDocument(
                    url="https://karnataka.gov.in/",
                    domain="karnataka.gov.in",
                    title="Karnataka State Portal",
                    description="Official portal.",
                    body_text="Karnataka is a state in south western India.",
                    quality_score=0.999,
                    digest="digest-1",
                )
            ]
        )
    finally:
        await client.close()

    requests = captured["body"]["requests"]
    # 0 BEGIN, 1 sites, 2 pages, 3/4 counts, 5 COMMIT
    page_args = requests[2]["stmt"]["args"]

    assert page_args[5] == {"type": "float", "value": 0.999}

    for index, arg in enumerate(page_args):
        if arg.get("type") == "float":
            assert isinstance(arg["value"], float), f"arg {index} sent a float as a string"
