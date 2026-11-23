"""The descriptor a browser installs must point somewhere a browser can reach.

A search-engine plugin sends the query itself, with no headers it did not read
out of this document. Pointing it at `/v1/search` made a plugin that answered
401 every time, because that path has required an API key since keys landed.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.engine.config import Settings
from app.main import opensearch


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.get("/opensearch.xml")(opensearch)
    return app


@pytest.mark.asyncio
async def test_the_template_points_at_the_page_when_one_is_configured():
    app = _app(Settings(environment="test", web_base_url="https://eyesearch.github.io/Eyesearch"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        body = (await http.get("/opensearch.xml")).text

    assert "https://eyesearch.github.io/Eyesearch/" in body
    assert "/v1/search?q={searchTerms}" not in body


@pytest.mark.asyncio
async def test_without_a_page_configured_it_still_describes_something():
    app = _app(Settings(environment="test"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        body = (await http.get("/opensearch.xml")).text

    assert "searchTerms" in body
    assert "<ShortName>eyesearch</ShortName>" in body


@pytest.mark.asyncio
async def test_the_service_index_lists_the_endpoints_it_serves():
    """`/` is the API's own table of contents; it was missing the newest two."""
    from app.main import root

    payload = await root()

    assert "/v1/answer?q=" in payload["endpoints"]
    assert "/v1/suggest" in payload["endpoints"]
    # `/v1/live` is gone with the live layer; the index lists what it serves.
    assert "/v1/live?key=" not in payload["endpoints"]
