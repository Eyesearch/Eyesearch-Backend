"""Agent 2 — the fetcher: async HTTP with pooling, timeouts and backoff.

One pooled client for a whole pass, a byte cap, and a retry that backs off on
transient failures. It never raises: every outcome is a `FetchResult`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from .headers import browser_headers
from app.crawler.crawler import crawl_client, fetch_page_status

logger = logging.getLogger(__name__)

TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(slots=True)
class FetchResult:
    """What the fetcher returns for one URL."""

    url: str
    html: str | None = None
    status: int | None = None
    error: str = ""
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.html is not None and self.status is not None and self.status < 400


class FetcherAgent:
    """Async entry point: `fetch(url)` and `fetch_many(urls)`."""

    name = "fetcher"

    def __init__(
        self,
        settings: Any | None = None,
        *,
        retries: int = 2,
        backoff: float = 0.4,
        fetch_fn: Any | None = None,
    ) -> None:
        self._settings = settings
        self.retries = max(1, retries)
        self.backoff = backoff
        # The low-level fetch is injectable: the pipeline supplies the same
        # function it always used, and tests can stub that seam.
        self._fetch_fn = fetch_fn or fetch_page_status
        self._client: Any | None = None

    @property
    def settings(self) -> Any:
        if self._settings is None:
            from app.engine.config import get_engine_settings

            self._settings = get_engine_settings()
        return self._settings

    def headers(self) -> dict[str, str]:
        return browser_headers(self.settings.crawl_user_agent)

    async def __aenter__(self) -> "FetcherAgent":
        self._client = crawl_client(self.settings)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str) -> FetchResult:
        """Fetch one URL, retrying transient failures with a short backoff."""
        client = self._client
        owned = False
        if client is None:
            client = crawl_client(self.settings)
            owned = True
        try:
            for attempt in range(1, self.retries + 1):
                try:
                    html, status = await self._fetch_fn(client, url, self.settings)
                except Exception as exc:  # never raise out of the fetcher
                    logger.warning("fetch failed for %s: %s", url, exc)
                    html, status = None, None

                transient = status is None or status in TRANSIENT_STATUS
                if (html is not None or status) and not transient:
                    return FetchResult(url=url, html=html, status=status, attempts=attempt)
                if attempt < self.retries and transient:
                    await asyncio.sleep(self.backoff * attempt)
                    continue
                return FetchResult(
                    url=url,
                    html=html,
                    status=status,
                    error="no response" if status is None else f"status {status}",
                    attempts=attempt,
                )
            return FetchResult(url=url, error="exhausted retries", attempts=self.retries)
        finally:
            if owned:
                await client.aclose()

    async def fetch_many(self, urls: list[str], *, concurrency: int = 5) -> list[FetchResult]:
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def one(url: str) -> FetchResult:
            async with semaphore:
                return await self.fetch(url)

        return await asyncio.gather(*(one(url) for url in urls))


__all__ = ["FetcherAgent", "FetchResult"]
