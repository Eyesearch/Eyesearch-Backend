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
from urllib.parse import quote, urlsplit, urlunsplit

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


#: Characters left alone when repairing a URL path. `%` is safe so an already
#: encoded path is not double-encoded.
_PATH_SAFE = "/%:@!$&'()*+,;=~-._"
_QUERY_SAFE = "=&%:@!$'()*+,;/?~-._"


def sanitise_url(url: str) -> str | None:
    """Make `url` something httpx will accept, or None if it cannot be repaired.

    A malformed href in a page — an unencoded space, a relative path with no
    leading slash, a broken IPv6 literal — otherwise raises `httpx.InvalidURL`,
    which is not an `HTTPError` and so escapes every handler below it. Returning
    None lets the caller record a clean failure instead.
    """
    if not url or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
        netloc = parts.netloc
    except ValueError:
        return None
    if not netloc:
        return None

    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path

    return urlunsplit(
        (
            parts.scheme or "https",
            netloc,
            quote(path, safe=_PATH_SAFE),
            quote(parts.query, safe=_QUERY_SAFE),
            parts.fragment,
        )
    )


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

    async def fetch(self, url: str, *, attempts: int = 1) -> FetchResult:
        """Fetch one URL, single pass by default.

        A retry costs a second full timeout while still holding a semaphore
        slot, and a batch waits for its slowest member — so one dead host used
        to stall twenty other URLs behind it. Failed URLs are marked `failed`
        and the frontier moves on, which is cheaper than waiting.

        There is no sleep between attempts either. The semaphore already paces
        requests, and sleeping inside it multiplied the stall.

        `attempts` is still honoured for callers that want a retry; the
        revalidation worker passes 1 explicitly.
        """
        started = time.monotonic()
        last_error = ""
        last_status = 0

        target = sanitise_url(url)
        if target is None:
            return FetchResult(
                url=url,
                ok=False,
                error="invalid_url",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        for attempt in range(1, max(1, attempts) + 1):
            async with self._semaphore:
                try:
                    self.fetches += 1
                    response = await self._client.get(target)
                except httpx.TimeoutException:
                    last_error = "timeout"
                except httpx.HTTPError as error:
                    last_error = f"http_error:{type(error).__name__}"
                except Exception as error:
                    # httpx.InvalidURL subclasses Exception, not HTTPError, so
                    # without this it escapes fetch(), kills the whole gather in
                    # fetch_many(), and stops the crawl loop for a full minute.
                    last_error = f"fetch_error:{type(error).__name__}"
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
                        # Missing/invalid pages are not transient. Avoid spending
                        # another timeout on ordinary 4xx responses, but retry
                        # server-side 5xx responses once when attempts > 1.
                        if 400 <= response.status_code < 500:
                            break
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
                logger.debug("retrying %s (attempt %d of %d)", url, attempt + 1, attempts)

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

    async def fetch_many(
        self, urls: list[str], *, attempts: int = 2
    ) -> list[FetchResult]:
        """Fetch a batch concurrently, retrying transient failures once by default.

        Timeouts, connection errors and 5xx responses get one bounded retry.
        Ordinary 4xx responses and blocked hosts do not.
        """
        if not urls:
            return []
        raw = await asyncio.gather(
            *(self.fetch(url, attempts=attempts) for url in urls),
            return_exceptions=True,
        )

        results: list[FetchResult] = []
        for url, item in zip(urls, raw):
            if isinstance(item, BaseException):
                # Belt and braces: fetch() already refuses to raise, but a bug
                # there must cost one URL, never the batch.
                logger.debug("fetch of %s raised %s", url, type(item).__name__)
                results.append(
                    FetchResult(url=url, ok=False, error=f"gather_error:{type(item).__name__}")
                )
            else:
                results.append(item)
        return results

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = ["BLOCKING_STATUSES", "BOUND_HEADERS", "FetchResult", "FetcherAgent"]
