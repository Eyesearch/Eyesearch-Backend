"""Live web retrieval with a bounded, degrading fallback chain.

Chain: DuckDuckGo (library) -> DuckDuckGo HTML endpoint -> DuckDuckGo Lite.

Design constraints for a single 512 MB Render instance:

* every hop has its own timeout, and the whole chain has a budget;
* a global semaphore caps concurrent live retrievals;
* a per-provider circuit breaker keeps a dead provider off the request path;
* a provider that fails never becomes a 500 — it becomes ``degraded: true``.

The DuckDuckGo library is synchronous, so it runs in a worker thread and is
awaited with a timeout; the scrape hops are pure ``httpx`` and stay async.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import httpx

from app.config import Settings, get_settings
from app.errors import ProviderError, RateLimitedError
from app.services.formatter import canonical_url

logger = logging.getLogger(__name__)

USER_AGENT = (
    "EyeSearchBot/1.0 (+https://github.com/Eyesearch/Eyesearch-Backend; "
    "unified search gateway; contact: yashwanthputtarajegowda@gmail.com)"
)

HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
LITE_ENDPOINT = "https://lite.duckduckgo.com/lite/"
BING_ENDPOINT = "https://www.bing.com/search"
DDG_REFERER = "https://duckduckgo.com/"

# The first three hops are always on. Bing is opt-in (ENABLE_BING_SCRAPE=false by
# default) because its terms discourage automated scraping — the owner decides.
PROVIDER_ORDER: tuple[str, ...] = (
    "duckduckgo",
    "duckduckgo_html",
    "duckduckgo_lite",
    "bing",
)


@dataclass(slots=True)
class RawResult:
    """A single provider hit, before formatting."""

    title: str
    url: str
    snippet: str = ""
    source: str = "unknown"
    position: int = 0
    published_at: str | None = None
    image_url: str | None = None
    sections: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class RetrievalOutcome:
    """What the chain managed to produce, and what it could not."""

    results: list[RawResult] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    providers: dict[str, int] = field(default_factory=dict)
    timed_out: bool = False

    @property
    def degraded(self) -> bool:
        return bool(self.failed)

    @property
    def failed_providers(self) -> list[str]:
        return [item.get("provider", "unknown") for item in self.failed]


class CircuitBreaker:
    """In-memory breaker: opens after N consecutive failures, half-opens later.

    Deliberately a plain dict — a shared Redis for four providers on a free tier
    is more moving parts than the problem needs.
    """

    def __init__(self, threshold: int = 5, reset_after_s: float = 60.0) -> None:
        self.threshold = max(1, threshold)
        self.reset_after_s = reset_after_s
        self._state: dict[str, dict[str, float]] = {}

    def _entry(self, provider: str) -> dict[str, float]:
        return self._state.setdefault(provider, {"failures": 0.0, "opened_at": 0.0})

    def is_open(self, provider: str) -> bool:
        entry = self._entry(provider)
        if entry["failures"] < self.threshold:
            return False
        if entry["opened_at"] == 0.0:
            entry["opened_at"] = time.monotonic()
            return True
        if time.monotonic() - entry["opened_at"] >= self.reset_after_s:
            # half-open: allow one attempt through
            entry["failures"] = self.threshold - 1
            entry["opened_at"] = 0.0
            return False
        return True

    def record_success(self, provider: str) -> None:
        entry = self._entry(provider)
        entry["failures"] = 0.0
        entry["opened_at"] = 0.0

    def record_failure(self, provider: str) -> None:
        entry = self._entry(provider)
        entry["failures"] += 1
        if entry["failures"] >= self.threshold:
            entry["opened_at"] = time.monotonic()

    def snapshot(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for provider in PROVIDER_ORDER:
            entry = self._entry(provider)
            out[provider] = "open" if self.is_open(provider) else "closed"
        return out


breaker = CircuitBreaker()
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore(settings: Settings) -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_live))
    return _semaphore


def provider_budget(provider: str, settings: Settings) -> float:
    return {
        "duckduckgo": settings.ddgs_budget_s,
        "duckduckgo_html": settings.html_budget_s,
        "duckduckgo_lite": settings.lite_budget_s,
        "bing": settings.bing_budget_s,
    }.get(provider, 2.5)


def provider_chain(settings: Settings) -> tuple[str, ...]:
    """The hops to attempt, in order, for this configuration."""
    if settings.enable_bing_scrape:
        return PROVIDER_ORDER
    return tuple(name for name in PROVIDER_ORDER if name != "bing")


def _unwrap_ddg_url(href: str) -> str:
    """DuckDuckGo wraps outbound links as ``/l/?uddg=<url>``; unwrap them."""
    if not href:
        return ""
    candidate = href.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    parsed = urlparse(candidate)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return target[0]
    return candidate


def _ddgs_sync(query: str, limit: int, region: str, safesearch: str) -> list[dict[str, Any]]:
    """Blocking library call — always invoked through ``asyncio.to_thread``."""
    try:  # the package was renamed; support both import paths
        from ddgs import DDGS  # type: ignore[import-not-found]
    except ImportError:
        from duckduckgo_search import DDGS  # type: ignore[import-not-found]

    with DDGS() as client:
        return list(
            client.text(
                query,
                region=region,
                safesearch=safesearch,
                max_results=max(1, min(limit, 50)),
            )
        )


async def _duckduckgo_provider(
    client: httpx.AsyncClient | None,
    query: str,
    limit: int,
    settings: Settings,
) -> list[RawResult]:
    try:
        payload = await asyncio.to_thread(
            _ddgs_sync, query, limit, settings.search_region, settings.safesearch
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # ratelimit, parse error, DNS, anything
        raise ProviderError(f"duckduckgo library failed: {exc}", provider="duckduckgo") from exc

    results: list[RawResult] = []
    for position, item in enumerate(payload or []):
        if not isinstance(item, dict):
            continue
        url = _unwrap_ddg_url(str(item.get("href") or item.get("url") or ""))
        title = str(item.get("title") or "").strip()
        if not url or not title:
            continue
        results.append(
            RawResult(
                title=title,
                url=url,
                snippet=str(item.get("body") or item.get("snippet") or "").strip(),
                source="duckduckgo",
                position=position,
                image_url=item.get("image") or None,
            )
        )
    return results[:limit]


def _parse_html_results(markup: str, source: str, limit: int) -> list[RawResult]:
    """Parse a DuckDuckGo HTML/Lite page. Defensive: HTML changes are not crashes."""
    from bs4 import BeautifulSoup  # imported here to keep cold start light

    soup = BeautifulSoup(markup, "html.parser")
    results: list[RawResult] = []

    for node in soup.select("div.result"):
        classes = node.get("class") or []
        if "result--ad" in classes:
            continue
        link = node.select_one("a.result__a") or node.select_one("a[href]")
        if link is None:
            continue
        url = _unwrap_ddg_url(str(link.get("href") or ""))
        title = link.get_text(" ", strip=True)
        if not url or not title:
            continue
        snippet_node = node.select_one(".result__snippet")
        snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
        results.append(
            RawResult(
                title=title,
                url=url,
                snippet=snippet,
                source=source,
                position=len(results),
            )
        )
        if len(results) >= limit:
            return results

    if results:
        return results

    # DuckDuckGo Lite markup: anchors with class result-link inside a table.
    for link in soup.select("a.result-link"):
        url = _unwrap_ddg_url(str(link.get("href") or ""))
        title = link.get_text(" ", strip=True)
        if not url or not title:
            continue
        snippet = ""
        row = link.find_parent("tr")
        if row is not None:
            snippet_node = row.select_one("td.result-snippet")
            if snippet_node is not None:
                snippet = snippet_node.get_text(" ", strip=True)
        results.append(
            RawResult(
                title=title,
                url=url,
                snippet=snippet,
                source=source,
                position=len(results),
            )
        )
        if len(results) >= limit:
            break
    return results


async def _scrape(
    client: httpx.AsyncClient,
    endpoint: str,
    query: str,
    limit: int,
    settings: Settings,
    source: str,
) -> list[RawResult]:
    try:
        response = await client.post(
            endpoint,
            data={"q": query, "kl": settings.search_region},
            headers={
                "User-Agent": USER_AGENT,
                "Referer": DDG_REFERER,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    except httpx.TimeoutException as exc:
        raise ProviderError(f"{source} timed out", provider=source) from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"{source} request failed: {exc}", provider=source) from exc

    if response.status_code == 429:
        raise RateLimitedError(f"{source} rate limited", retryable=True)
    if response.status_code in (202, 403):
        # DuckDuckGo answers non-browser clients with 202 + a challenge page.
        raise ProviderError(
            f"{source} blocked the request (HTTP {response.status_code})", provider=source
        )
    if response.status_code >= 400:
        raise ProviderError(
            f"{source} returned HTTP {response.status_code}", provider=source
        )
    try:
        parsed = _parse_html_results(response.text, source, limit)
    except Exception as exc:
        raise ProviderError(f"{source} markup unparseable: {exc}", provider=source) from exc
    if not parsed:
        raise ProviderError(f"{source} returned no parseable results", provider=source)
    return parsed


async def _html_provider(
    client: httpx.AsyncClient | None, query: str, limit: int, settings: Settings
) -> list[RawResult]:
    if client is None:
        raise ProviderError("no http client", provider="duckduckgo_html")
    return await _scrape(client, HTML_ENDPOINT, query, limit, settings, "duckduckgo_html")


def _decode_bing_url(href: str) -> str:
    """Bing wraps outbound links as ``/ck/a?...&u=a1<base64url>``; decode the target."""
    if not href:
        return ""
    candidate = href.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    parsed = urlparse(candidate)
    if "bing.com" not in parsed.netloc or not parsed.path.startswith("/ck/"):
        return candidate
    token = parse_qs(parsed.query).get("u")
    if not token:
        return candidate
    raw = token[0]
    if raw.startswith("a1"):
        raw = raw[2:]
    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8", "ignore")
    except (binascii.Error, ValueError):
        return candidate
    return decoded if decoded.startswith("http") else candidate


def _parse_bing_results(markup: str, limit: int) -> list[RawResult]:
    """Parse a Bing results page. Defensive: markup changes are ProviderError, not crashes."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")
    results: list[RawResult] = []
    for node in soup.select("li.b_algo"):
        link = node.select_one("h2 a") or node.select_one("a[href]")
        if link is None:
            continue
        url = _decode_bing_url(str(link.get("href") or ""))
        title = link.get_text(" ", strip=True)
        if not url.startswith("http") or not title:
            continue
        snippet_node = node.select_one("p") or node.select_one(".b_caption")
        snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
        results.append(
            RawResult(
                title=title,
                url=url,
                snippet=snippet,
                source="bing",
                position=len(results),
            )
        )
        if len(results) >= limit:
            break
    return results


async def _bing_provider(
    client: httpx.AsyncClient | None, query: str, limit: int, settings: Settings
) -> list[RawResult]:
    if client is None:
        raise ProviderError("no http client", provider="bing")
    try:
        response = await client.get(
            BING_ENDPOINT,
            params={"q": query, "cc": "in", "setlang": "en", "count": str(max(limit, 10))},
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.9",
            },
        )
    except httpx.TimeoutException as exc:
        raise ProviderError("bing timed out", provider="bing") from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"bing request failed: {exc}", provider="bing") from exc

    if response.status_code == 429:
        raise RateLimitedError("bing rate limited", retryable=True)
    if response.status_code >= 400:
        raise ProviderError(f"bing returned HTTP {response.status_code}", provider="bing")
    try:
        parsed = _parse_bing_results(response.text, limit)
    except Exception as exc:
        raise ProviderError(f"bing markup unparseable: {exc}", provider="bing") from exc
    if not parsed:
        raise ProviderError("bing returned no parseable results", provider="bing")
    return parsed


async def _lite_provider(
    client: httpx.AsyncClient | None, query: str, limit: int, settings: Settings
) -> list[RawResult]:
    if client is None:
        raise ProviderError("no http client", provider="duckduckgo_lite")
    return await _scrape(client, LITE_ENDPOINT, query, limit, settings, "duckduckgo_lite")


ProviderFn = Callable[
    [httpx.AsyncClient | None, str, int, Settings], Awaitable[list[RawResult]]
]

PROVIDERS: dict[str, ProviderFn] = {
    "duckduckgo": _duckduckgo_provider,
    "duckduckgo_html": _html_provider,
    "duckduckgo_lite": _lite_provider,
    "bing": _bing_provider,
}


def _temporary_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.http_connect_timeout_s,
            read=settings.http_read_timeout_s,
            write=settings.http_write_timeout_s,
            pool=settings.http_pool_timeout_s,
        ),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive,
        ),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


async def retrieve(
    query: str,
    limit: int,
    *,
    client: httpx.AsyncClient | None = None,
    settings: Settings | None = None,
) -> RetrievalOutcome:
    """Walk the fallback chain and return whatever survives.

    Never raises for a provider problem: failures are collected into
    ``RetrievalOutcome.failed`` and the caller returns a degraded response.
    """
    cfg = settings or get_settings()
    outcome = RetrievalOutcome()
    if not query.strip():
        return outcome

    owned_client: httpx.AsyncClient | None = None
    http = client
    if http is None:
        owned_client = _temporary_client(cfg)
        http = owned_client

    seen: set[str] = set()
    try:
        async with _get_semaphore(cfg):
            try:
                async with asyncio.timeout(cfg.chain_budget_s):
                    for provider in provider_chain(cfg):
                        if len(outcome.results) >= limit:
                            break
                        if breaker.is_open(provider):
                            outcome.failed.append(
                                {"provider": provider, "error": "circuit_open"}
                            )
                            continue
                        budget = provider_budget(provider, cfg)
                        try:
                            async with asyncio.timeout(budget):
                                batch = await PROVIDERS[provider](http, query, limit, cfg)
                        except (TimeoutError, asyncio.TimeoutError):
                            breaker.record_failure(provider)
                            outcome.failed.append({"provider": provider, "error": "timeout"})
                            logger.warning("%s exceeded %.2fs", provider, budget)
                            continue
                        except ProviderError as exc:
                            breaker.record_failure(provider)
                            outcome.failed.append(
                                {"provider": provider, "error": type(exc).__name__}
                            )
                            logger.warning("%s failed: %s", provider, exc)
                            continue
                        except RateLimitedError as exc:
                            breaker.record_failure(provider)
                            outcome.failed.append(
                                {"provider": provider, "error": "rate_limited"}
                            )
                            logger.warning("%s rate limited: %s", provider, exc)
                            continue
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # defensive: a provider must not 500 us
                            breaker.record_failure(provider)
                            outcome.failed.append(
                                {"provider": provider, "error": type(exc).__name__}
                            )
                            logger.exception("unexpected %s error", provider)
                            continue

                        breaker.record_success(provider)
                        accepted = 0
                        for item in batch:
                            key = canonical_url(item.url)
                            if not key or key in seen:
                                continue
                            seen.add(key)
                            outcome.results.append(item)
                            accepted += 1
                            if len(outcome.results) >= limit:
                                break
                        if accepted:
                            outcome.providers[provider] = accepted
            except (TimeoutError, asyncio.TimeoutError):
                outcome.timed_out = True
                outcome.failed.append({"provider": "chain", "error": "budget_exhausted"})
                logger.warning("retrieval chain exhausted %.1fs budget", cfg.chain_budget_s)
    finally:
        if owned_client is not None:
            await owned_client.aclose()

    return outcome


def provider_states(settings: Settings | None = None) -> dict[str, str]:
    """Breaker snapshot for /health, limited to the configured hops."""
    cfg = settings or get_settings()
    chain = provider_chain(cfg)
    return {name: state for name, state in breaker.snapshot().items() if name in chain}
