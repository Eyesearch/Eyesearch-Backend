"""POST /v1/chat — the AI Mode stream, in both grounded and LLM modes."""

from __future__ import annotations

import json

import httpx
import pytest

from app import database
from app.config import Settings
from app.engine.store import get_store
from app.main import app


@pytest.fixture()
async def temp_db(tmp_path, monkeypatch):
    settings = Settings(turso_db_url=f"file:{tmp_path}/chat.db")
    await database.connect(settings)
    await database.initialize_schema()
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    yield
    await database.close()


def _events(body: str) -> list[dict]:
    events = []
    for line in body.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))
    return events


async def test_grounded_mode_streams_an_answer_from_the_index(temp_db) -> None:
    store = get_store()
    await store.ingest_documents(
        [
            {"url": "https://eci.gov.in/", "canonical_url": "https://eci.gov.in/",
             "title": "Election Commission of India",
             "description": "Conducts elections and publishes voter rolls.",
             "payload_bytes": 120}
        ]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=20) as client:
        async with client.stream("POST", "/v1/chat", json={"query": "election commission"}) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = "".join([chunk async for chunk in response.aiter_text()])

    events = _events(body)
    assert events[0]["meta"]["mode"] == "grounded"
    text = "".join(event.get("delta", "") for event in events)
    assert "Election Commission of India" in text
    assert "eci.gov.in" in text

    done = events[-1]
    assert done["done"] is True
    assert done["sources"][0]["url"] == "https://eci.gov.in/"


async def test_an_unknown_query_says_so_instead_of_inventing(temp_db) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=20) as client:
        async with client.stream("POST", "/v1/chat", json={"query": "quantum basket weaving"}) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

    text = "".join(event.get("delta", "") for event in _events(body))
    assert "do not have indexed pages" in text
    assert _events(body)[-1]["sources"] == []


async def test_llm_mode_is_used_when_configured(temp_db, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.in/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        payload = json.loads(request.read())
        seen["model"] = payload["model"]
        seen["system"] = payload["messages"][0]["content"]
        body = (
            'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"from the model."}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    transport = httpx.ASGITransport(app=app)
    async with real_client(transport=transport, base_url="http://t", timeout=20) as client:
        async with client.stream("POST", "/v1/chat", json={"query": "hello"}) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

    events = _events(body)
    assert events[0]["meta"]["mode"] == "llm"
    text = "".join(event.get("delta", "") for event in events)
    assert text == "Hello from the model."
    assert seen["url"] == "https://llm.example.in/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["model"] == "test-model"


async def test_a_failing_llm_falls_back_to_the_grounded_answer(temp_db, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.in/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream is down")

    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    store = get_store()
    await store.ingest_documents(
        [{"url": "https://ssc.nic.in/", "canonical_url": "https://ssc.nic.in/",
          "title": "Staff Selection Commission", "description": "Recruitment notices.",
          "payload_bytes": 100}]
    )

    transport = httpx.ASGITransport(app=app)
    async with real_client(transport=transport, base_url="http://t", timeout=20) as client:
        async with client.stream("POST", "/v1/chat", json={"query": "staff selection"}) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

    text = "".join(event.get("delta", "") for event in _events(body))
    assert "Staff Selection Commission" in text
    assert _events(body)[-1]["done"] is True


async def test_follow_up_messages_are_accepted(temp_db) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=20) as client:
        async with client.stream(
            "POST",
            "/v1/chat",
            json={
                "query": "eci",
                "messages": [
                    {"role": "user", "content": "eci"},
                    {"role": "assistant", "content": "The Election Commission."},
                    {"role": "user", "content": "and its address?"},
                ],
            },
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

    events = _events(body)
    assert events[0]["meta"]["query"] == "eci"
    assert events[-1]["done"] is True
