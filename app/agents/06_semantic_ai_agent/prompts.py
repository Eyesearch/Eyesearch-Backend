"""Prompt templates for the semantic agent (Indoone AI, when it is wired)."""

from __future__ import annotations

SYSTEM_PROMPT = (
    "You are EyeSearch's assistant. Answer using the supplied search context when it "
    "is relevant, say plainly when it is not, cite pages by title and URL, and keep "
    "the reply concise markdown."
)

GROUNDING_TEMPLATE = """Search context:
{context}

Question: {question}"""

SUMMARY_TEMPLATE = """Summarise the following page in at most three sentences,
keeping only facts a reader could verify from the text:

Title: {title}
URL: {url}
Text: {body}"""


def render_context(documents: list[dict[str, str]], limit: int = 6) -> str:
    lines = []
    for item in documents[:limit]:
        title = item.get("title") or item.get("url") or ""
        url = item.get("url") or ""
        snippet = item.get("snippet") or ""
        lines.append(f"- {title} ({url}): {snippet}")
    return "\n".join(lines) or "(no indexed context for this query)"


def build_grounding_prompt(question: str, documents: list[dict[str, str]]) -> str:
    return GROUNDING_TEMPLATE.format(context=render_context(documents), question=question)


__all__ = ["SYSTEM_PROMPT", "GROUNDING_TEMPLATE", "SUMMARY_TEMPLATE", "render_context", "build_grounding_prompt"]
