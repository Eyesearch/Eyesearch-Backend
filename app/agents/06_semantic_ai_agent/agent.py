"""Agent 6 — the semantic layer: a plug-in seam for Indoone AI.

The hooks are real coroutines with a defined contract, but no model is called
yet: `answer()` returns a grounded digest of the retrieved documents and
`enabled` is False until `SEMANTIC_AGENT_ENABLED` and an endpoint are set. Wiring
the model later means implementing `_call_model()` — nothing else in the pipeline
changes.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .prompts import build_grounding_prompt, render_context

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SemanticResult:
    text: str
    sources: list[dict[str, str]] = field(default_factory=list)
    mode: str = "grounded"


class SemanticAIAgent:
    """Async entry point: `answer(question, documents)` and `stream(...)`."""

    name = "semantic"

    def __init__(self, settings: Any | None = None) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        """True once a model endpoint and key are configured."""
        return bool(os.getenv("LLM_BASE_URL")) and bool(os.getenv("LLM_API_KEY"))

    def prompt(self, question: str, documents: list[dict[str, str]]) -> str:
        return build_grounding_prompt(question, documents)

    async def _call_model(self, prompt: str) -> str:  # pragma: no cover - not wired
        """Where Indoone AI goes. Deliberately unimplemented until it exists."""
        raise NotImplementedError("Indoone AI endpoint is not configured")

    async def answer(
        self,
        question: str,
        documents: list[dict[str, str]] | None = None,
        **_: Any,
    ) -> SemanticResult:
        """A grounded answer from the retrieved documents."""
        docs = list(documents or [])
        if not docs:
            return SemanticResult(
                text=f"I do not have indexed pages for **{question}** yet.", sources=[]
            )
        body = render_context(docs)
        return SemanticResult(
            text=f"Here is what the index holds for **{question}**:\n\n{body}",
            sources=docs[:6],
            mode="llm" if self.enabled else "grounded",
        )

    async def stream(
        self,
        question: str,
        documents: list[dict[str, str]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield the answer in chunks, then a final `done` event."""
        result = await self.answer(question, documents, **kwargs)
        size = 24
        for index in range(0, len(result.text), size):
            yield {"delta": result.text[index : index + size]}
        yield {"done": True, "sources": result.sources}


__all__ = ["SemanticAIAgent", "SemanticResult"]
