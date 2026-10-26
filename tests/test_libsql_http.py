"""Parsing Turso/LibSQL HTTP responses — the shapes that raised KeyError 'result'."""

from __future__ import annotations

import httpx
import pytest

from app.errors import DatabaseError
from app.libsql_http import TursoHTTPClient, parse_pipeline_response


def test_pipeline_v2_step_shape() -> None:
    payload = {
        "results": [
            {
                "type": "ok",
                "response": {
                    "type": "execute",
                    "result": {
                        "cols": [{"name": "domain"}, {"name": "pages"}],
                        "rows": [[{"type": "text", "value": "india.gov.in"},
                                  {"type": "integer", "value": "6"}]],
                        "affected_row_count": 0,
                    },
                },
            }
        ]
    }
    result = parse_pipeline_response(payload)[0]
    assert result.columns == ("domain", "pages")
    assert result.rows == (("india.gov.in", 6),)


def test_single_result_shape() -> None:
    payload = {"result": {"cols": [{"name": "n"}], "rows": [[{"type": "integer", "value": "3"}]]}}
    result = parse_pipeline_response(payload)[0]
    assert result.rows == ((3,),)
    assert result.columns == ("n",)


def test_a_pipeline_yields_one_result_per_step() -> None:
    """`results` is a list of steps: unpack it, one ResultSet each."""
    payload = {
        "results": [
            {"cols": [{"name": "a"}], "rows": [[{"type": "text", "value": "x"}]]},
            {"cols": [{"name": "a"}], "rows": [[{"type": "text", "value": "y"}]]},
        ]
    }
    results = parse_pipeline_response(payload)
    assert len(results) == 2
    assert results[0].rows == (("x",),)
    assert results[1].rows == (("y",),)
    assert results[0].columns == ("a",)


def test_insert_without_a_rowset_is_success() -> None:
    payload = {
        "results": [
            {"type": "ok", "response": {"type": "execute", "result": {"affected_row_count": 1}}}
        ]
    }
    result = parse_pipeline_response(payload)[0]
    assert result.rows == ()
    assert result.columns == ()


def test_missing_keys_never_raise_keyerror() -> None:
    for payload in ({}, {"results": []}, {"unexpected": True}, None, "text"):
        result = parse_pipeline_response(payload)[0]
        assert result.rows == ()


def test_error_step_raises_a_database_error() -> None:
    payload = {"results": [{"type": "error", "error": {"message": "no such table: crawl_backlog"}}]}
    with pytest.raises(DatabaseError):
        parse_pipeline_response(payload)


async def test_client_executes_and_batches_over_the_pipeline() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read()
        import json

        body = json.loads(payload)
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "type": "ok",
                        "response": {
                            "type": "execute",
                            "result": {
                                "cols": [{"name": "domain"}],
                                "rows": [[{"type": "text", "value": "india.gov.in"}]],
                            },
                        },
                    }
                    for _ in body["requests"]
                ]
            },
        )

    client = TursoHTTPClient(
        "https://db.turso.io",
        auth_token="token",
        transport=httpx.MockTransport(handler),
    )

    result = await client.execute("SELECT domain FROM sites WHERE site_id = ?", [7])
    assert result.rows == (("india.gov.in",),)
    assert seen[0]["requests"][0]["stmt"]["args"] == [{"type": "integer", "value": "7"}]

    results = await client.batch([("INSERT INTO sites (domain) VALUES (?)", ["a.in"]), "SELECT 1"])
    assert len(results) == 2
    assert len(seen[1]["requests"]) == 2
    await client.close()


async def test_client_surfaces_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    client = TursoHTTPClient("https://db.turso.io", transport=httpx.MockTransport(handler))
    with pytest.raises(DatabaseError):
        await client.execute("SELECT 1")
    await client.close()
