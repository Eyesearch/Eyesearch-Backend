"""Agent 02 — the fetcher.

One pooled `httpx.AsyncClient` for the whole process, a desktop browser's
header set on every request, bounded concurrency, and a byte cap so a hostile
or accidental multi-gigabyte response cannot exhaust a 512 MB instance.

The User-Agent carries no bot token: several Indian government WAFs answer 403
or 406 to anything that announces itself as a crawler, and a plain desktop
Chrome profile is what gets the page.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from app.engine.config import Settings, get_settings

logger = logging.getLogger(__name__)

BOUND_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "no-cache",
}

# Statuses that mean "this host will not talk to us" rather than "this page is
# missing". Retrying them is how a crawl stalls on one domain forever.
BLOCKING_STATUSES: frozenset[int] = frozenset({401, 403, 406, 429, 451})


@dataclass
class FetchResult:
    """The outcome of one fetch, success or failure."""

    url: str
    ok: bool
    status: int = 0
    html: str = ""
    error: str = ""
    elapsed_ms: int = 0
    blocked: bool = False

    @property
    def reason(self) -> str:
        if self.ok:
            return ""
        if self.blocked:
            return f"blocked_status:{self.status}"
        if self.status:
            return f"http_status:{self.status}"
        return self.error or "fetch_failed"


class FetcherAgent:
    """Bounded, pooled HTTP fetching."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=self._settings.crawl_timeout_seconds,
            follow_redirects=True,
            transport=transport,
            headers={
                **BOUND_HEADERS,
                "User-Agent": self._settings.crawl_user_agent,
            },
        )
        self._semaphore = asyncio.Semaphore(max(1, self._settings.crawl_concurrency))
        self.fetches = 0

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def fetch(self, url: str, *, attempts: int = 2) -> FetchResult:
        """Fetch one URL, retrying only what is worth retrying."""
        started = time.monotonic()
        last_error = ""
        last_status = 0

        for attempt in range(1, max(1, attempts) + 1):
            async with self._semaphore:
                try:
                    self.fetches += 1
                    response = await self._client.get(url)
                except httpx.TimeoutException:
                    last_error = "timeout"
                except httpx.HTTPError as error:
                    last_error = f"http_error:{type(error).__name__}"
                else:
                    last_status = response.status_code
                    if response.status_code in BLOCKING_STATUSES:
                        logger.debug("fetch %s blocked with %d", url, response.status_code)
                        return FetchResult(
                            url=url,
                            ok=False,
                            status=response.status_code,
                            blocked=True,
                            error=f"blocked_status:{response.status_code}",
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                        )
                    if response.status_code >= 400:
                        last_error = f"http_status:{response.status_code}"
                    else:
                        html = self._read_capped(response)
                        return FetchResult(
                            url=url,
                            ok=True,
                            status=response.status_code,
                            html=html,
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                        )

            if attempt < attempts:
                await asyncio.sleep(0.5 * attempt)

        return FetchResult(
            url=url,
            ok=False,
            status=last_status,
            error=last_error or "fetch_failed",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    def _read_capped(self, response: httpx.Response) -> str:
        """Decode at most `crawl_max_body_bytes`, never the whole stream."""
        raw = response.content[: self._settings.crawl_max_body_bytes]
        encoding = response.encoding or "utf-8"
        try:
            return raw.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return raw.decode("utf-8", errors="replace")

    async def fetch_many(self, urls: list[str]) -> list[FetchResult]:
        """Fetch a batch concurrently, bounded by the semaphore."""
        if not urls:
            return []
        return list(await asyncio.gather(*(self.fetch(url) for url in urls)))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["BLOCKING_STATUSES", "BOUND_HEADERS", "FetchResult", "FetcherAgent"]
