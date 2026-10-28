"""Agent 09 — robots.txt.

A bounded, cached robots.txt checker. Three rules shape it:

**Fail open, politely.** A missing, slow, erroring or unparseable robots.txt
means "crawl", not "refuse". The per-domain page cap is what bounds the damage;
refusing to crawl because a 2-second fetch timed out would silently drop whole
sites for no reason.

**Bounded in every dimension.** One fetch per host, cached for an hour, at most
`max_entries` hosts held, at most `max_concurrent` fetches in flight, and a hard
`timeout` on each request. A crawler that leaks a cache entry per host or opens a
socket per URL is how you run a 512 MB instance out of memory.

**Cheap when it matters.** The crawl loop asks about every URL, so a cache hit
must be a dictionary lookup. The first URL of a host pays for the fetch; every
other URL of that host — including across batches — is free.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: A robots.txt larger than this is not worth parsing; treat it as absent.
MAX_ROBOTS_BYTES = 500_000


@dataclass(frozen=True)
class RobotsRules:
    """The Allow/Disallow rules that apply to us, in declaration order."""

    allow: tuple[str, ...] = ()
    disallow: tuple[str, ...] = ()

    def allows(self, path: str) -> bool:
        """Longest matching rule wins; Allow wins a tie, as the standard says."""
        target = path or "/"
        best_allow = -1
        best_disallow = -1

        for pattern in self.allow:
            if _matches(pattern, target):
                best_allow = max(best_allow, len(pattern))

        for pattern in self.disallow:
            if _matches(pattern, target):
                best_disallow = max(best_disallow, len(pattern))

        if best_disallow < 0:
            return True
        if best_allow < 0:
            return False
        return best_allow >= best_disallow


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """robots.txt patterns are prefixes with `*` wildcards and an optional `$`."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    expression = ".*".join(re.escape(part) for part in body.split("*"))
    expression = "^" + expression + ("$" if anchored else "")
    return re.compile(expression)


def _matches(pattern: str, path: str) -> bool:
    if pattern == "":
        return False
    try:
        return bool(_pattern_to_regex(pattern).match(path))
    except re.error:
        return False


def parse_robots(document: str, user_agent: str = "") -> RobotsRules:
    """The rules for `*` and for any group naming a token in `user_agent`."""
    allow: list[str] = []
    disallow: list[str] = []

    agents: list[str] = []
    collecting = False
    seen_rule = False

    for raw in (document or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()

        if key == "user-agent":
            if seen_rule:
                # A new group starts once a rule has been seen.
                agents = []
                seen_rule = False
            agents.append(value.lower())
            collecting = False
            continue

        if key not in ("allow", "disallow"):
            continue

        seen_rule = True
        if not collecting:
            collecting = _group_applies(agents, user_agent)

        if not collecting:
            continue
        if not value:
            # "Disallow:" with no path means nothing is disallowed.
            continue
        if key == "allow":
            allow.append(value)
        else:
            disallow.append(value)

    return RobotsRules(allow=tuple(allow), disallow=tuple(disallow))


def _group_applies(agents: list[str], user_agent: str) -> bool:
    if not agents:
        return False
    lowered = (user_agent or "").lower()
    for agent in agents:
        if agent == "*":
            return True
        if agent and agent in lowered:
            return True
    return False


@dataclass
class _Entry:
    expires_at: float
    rules: RobotsRules


class RobotsCache:
    """Per-host robots.txt, cached, bounded and fail-open."""

    def __init__(
        self,
        client: Any,
        *,
        user_agent: str = "",
        timeout: float = 2.0,
        max_entries: int = 2000,
        ttl_seconds: float = 3600.0,
        fail_ttl_seconds: float = 300.0,
        max_concurrent: int = 10,
        clock: Any = None,
    ) -> None:
        self._client = client
        self._user_agent = user_agent
        self._timeout = max(0.1, timeout)
        self._max_entries = max(1, max_entries)
        self._ttl = ttl_seconds
        self._fail_ttl = fail_ttl_seconds
        self._clock = clock or time.monotonic
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[RobotsRules]] = {}
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self.fetches = 0
        self.failures = 0

    # -- public ------------------------------------------------------------

    async def allowed(self, url: str) -> bool:
        """Whether we may fetch `url`. Never raises."""
        parts = urlparse(url or "")
        host = (parts.hostname or "").lower()
        if not host:
            return True

        key = f"{parts.scheme or 'https'}://{host}"
        rules = await self._rules_for(key, host)
        return rules.allows(parts.path or "/")

    def __len__(self) -> int:
        return len(self._entries)

    # -- internals ---------------------------------------------------------

    async def _rules_for(self, key: str, host: str) -> RobotsRules:
        cached = self._entries.get(key)
        if cached is not None:
            if cached.expires_at > self._clock():
                self._entries.move_to_end(key)
                return cached.rules
            self._entries.pop(key, None)

        pending = self._inflight.get(key)
        if pending is not None:
            # Another caller is already fetching this host; wait for its answer
            # instead of issuing a second request.
            return await asyncio.shield(pending)

        future: asyncio.Future[RobotsRules] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            rules, ttl = await self._fetch(key)
        except Exception as error:  # never let robots take the crawl down
            self.failures += 1
            logger.debug("robots.txt for %s failed (%s); allowing", host, error)
            rules, ttl = RobotsRules(), self._fail_ttl
        finally:
            self._inflight.pop(key, None)

        self._store(key, rules, ttl)
        if not future.done():
            future.set_result(rules)
        return rules

    async def _fetch(self, key: str) -> tuple[RobotsRules, float]:
        async with self._semaphore:
            self.fetches += 1
            response = await self._client.get(
                f"{key}/robots.txt", timeout=self._timeout
            )

        status = getattr(response, "status_code", 0)
        if status != 200:
            # 4xx/5xx: there is no robots.txt to obey.
            return RobotsRules(), self._fail_ttl

        text = (getattr(response, "text", "") or "")[:MAX_ROBOTS_BYTES]
        return parse_robots(text, self._user_agent), self._ttl

    def _store(self, key: str, rules: RobotsRules, ttl: float) -> None:
        self._entries[key] = _Entry(expires_at=self._clock() + ttl, rules=rules)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


__all__ = [
    "MAX_ROBOTS_BYTES",
    "RobotsCache",
    "RobotsRules",
    "parse_robots",
]
