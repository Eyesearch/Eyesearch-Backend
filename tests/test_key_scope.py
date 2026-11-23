"""Which paths need a key, and which cannot.

The web client is a static page: it cannot keep a secret, so a key in it is a
published key. It calls `/v1/search` and `/v1/suggest` — those stay open and are
held to a per-address limit instead. The machine contract needs a key.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.engine.config import Settings
from app.main import KEYED_PREFIXES, require_api_key


def _needs_key(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in KEYED_PREFIXES)


@pytest.mark.parametrize("path", ["/v1/search", "/v1/suggest", "/v1/chat"])
def test_the_browser_paths_do_not_need_a_key(path):
    """The web client sends no headers, so a key here is a published key.

    `/v1/chat` was left keyed when `/v1/search` was opened, and the AI Mode
    panel posts to it from the browser — so the panel answered 401 to every
    reader while the rest of the site worked.
    """
    assert _needs_key(path) is False


@pytest.mark.parametrize(
    "path",
    ["/v1/answer", "/v1/live", "/v1/retrieve", "/v1/ai/search", "/v1/stats"],
)
def test_the_machine_paths_need_a_key(path):
    assert _needs_key(path) is True


@pytest.mark.asyncio
async def test_a_database_fault_in_the_middleware_still_returns_the_envelope():
    """Middleware runs before the handlers, so its faults bypass their handlers.

    A bare 500 has no `code`, and the frontend maps `code` onto what it shows.
    """
    app = FastAPI()
    app.middleware("http")(require_api_key)

    class BrokenClient:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("connection refused")

    app.state.client = BrokenClient()

    @app.get("/v1/answer")
    async def answer():
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get("/v1/answer", headers={"X-API-Key": "anything"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_error"


def test_the_statement_timeout_is_long_enough_for_the_crawl_to_run():
    """Four seconds killed every crawl batch on its first query.

    The queue's dequeue is a window function over tens of thousands of rows; on
    this box it takes longer than four seconds, so the loop logged "crawl loop
    error, continuing" and continued to do nothing.
    """
    from app.engine.config import Settings

    assert Settings(_env_file=None).db_statement_timeout_ms >= 20000
