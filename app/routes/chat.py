"""POST /v1/chat — the streaming endpoint behind the "AI Mode" tab.

Two modes, chosen by configuration:

* **LLM mode** — when `LLM_BASE_URL` and `LLM_API_KEY` are set, the request is
  forwarded to that OpenAI-compatible chat-completions endpoint with our own
  index results injected as grounding context, and the tokens are streamed
  straight back to the browser.
* **Grounded mode** — with no LLM configured, the answer is composed from the
  retrieved documents themselves: a markdown summary of the most relevant pages
  with their links. No model is called and nothing is invented.

Both stream Server-Sent Events: `data: {"delta": "..."}` chunks, then
`data: {"done": true, "sources": [...]}`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.engine.store import get_store
from app.services import formatter

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

SYSTEM_PROMPT = (
    "You are EyeSearch's assistant. Answer the user's question using the provided "
    "search context when it is relevant, and say plainly when the context does not "
    "cover the question. Cite pages by their title and URL. Be concise and use "
    "markdown."
)

MAX_CONTEXT_DOCUMENTS = 6


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str


class ChatRequest(BaseModel):
    query: str = ""
    messages: list[ChatMessage] = Field(default_factory=list)
    limit: int = Field(default=MAX_CONTEXT_DOCUMENTS, ge=1, le=12)


def _llm_config() -> dict[str, str] | None:
    base = (os.getenv("LLM_BASE_URL") or "").strip()
    key = (os.getenv("LLM_API_KEY") or "").strip()
    if not base or not key:
        return None
    return {
        "base": base.rstrip("/"),
        "key": key,
        "model": (os.getenv("LLM_MODEL") or "gpt-4o-mini").strip(),
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _context(query: str, limit: int) -> list[dict[str, str]]:
    """Our own index results, used as grounding for either mode."""
    if not query:
        return []
    try:
        store = get_store()
        hits = await store.search_local(query, limit=limit)
    except Exception as exc:
        logger.warning("chat context lookup failed: %s", exc)
        return []
    return [
        {
            "title": hit.title or hit.url,
            "url": hit.url,
            "snippet": hit.snippet or "",
        }
        for hit in hits
    ]


def _grounded_answer(query: str, context: list[dict[str, str]]) -> str:
    """A markdown answer built only from what we indexed."""
    if not context:
        return (
            f"I do not have indexed pages for **{query}** yet.\n\n"
            "The crawler adds Indian public sources continuously — try again shortly, "
            "or rephrase the question."
        )

    lines = [f"Here is what the EyeSearch index holds for **{query}**:", ""]
    for item in context:
        snippet = item["snippet"].strip()
        title = item["title"]
        lines.append(f"- **[{title}]({item['url']})**" + (f" — {snippet}" if snippet else ""))
    lines.append("")
    lines.append(
        "These are extracts from our own crawl, not generated prose. Ask a follow-up "
        "to narrow it down."
    )
    return "\n".join(lines)


def _chunks(text: str, size: int = 24) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


async def _stream_llm(
    cfg: dict[str, str],
    query: str,
    messages: list[ChatMessage],
    context: list[dict[str, str]],
) -> AsyncIterator[str]:
    """Forward to an OpenAI-compatible endpoint and relay its deltas."""
    grounding = "\n".join(
        f"- {item['title']} ({item['url']}): {item['snippet']}" for item in context
    ) or "(no indexed context for this query)"
    conversation = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\nSearch context:\n{grounding}"}]
    conversation += [message.model_dump() for message in messages[-8:]]
    if not messages:
        conversation.append({"role": "user", "content": query})

    payload = {
        "model": cfg["model"],
        "messages": conversation,
        "stream": True,
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST", f"{cfg['base']}/chat/completions", json=payload, headers=headers
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "ignore")[:300]
                    logger.error("LLM endpoint %s: %s", response.status_code, body)
                    raise RuntimeError(f"LLM endpoint returned {response.status_code}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content")
                    if delta:
                        yield _sse({"delta": delta})
    except Exception as exc:
        logger.warning("LLM stream failed (%s) — falling back to the grounded answer", exc)
        for piece in _chunks(_grounded_answer(query, context)):
            yield _sse({"delta": piece})
            await asyncio.sleep(0)


async def _events(request: ChatRequest) -> AsyncIterator[str]:
    query = (request.query or "").strip()
    if not query and request.messages:
        query = next(
            (m.content for m in reversed(request.messages) if m.role == "user"), ""
        ).strip()

    context = await _context(query, request.limit)
    cfg = _llm_config()

    yield _sse({"meta": {"mode": "llm" if cfg else "grounded", "query": query, "model": cfg["model"] if cfg else None}})

    if cfg:
        async for chunk in _stream_llm(cfg, query, request.messages, context):
            yield chunk
    else:
        for piece in _chunks(_grounded_answer(query, context)):
            yield _sse({"delta": piece})
            await asyncio.sleep(0.01)

    yield _sse(
        {
            "done": True,
            "sources": [
                {
                    "title": item["title"],
                    "url": item["url"],
                    "favicon": formatter.canonical_url(item["url"]) + "/favicon.ico",
                }
                for item in context
            ],
        }
    )


@router.post("/v1/chat", summary="Streaming conversational answer (AI Mode)")
async def chat(request: ChatRequest) -> StreamingResponse:
    """SSE stream of markdown deltas, then a `done` event carrying sources."""
    return StreamingResponse(
        _events(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["router", "ChatRequest", "ChatMessage"]
