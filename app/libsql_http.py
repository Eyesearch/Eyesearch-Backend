"""The one path to the database.

Two things live here:

* `ProcessRateLimiter` — a single process-wide throttle so the crawler, the
  revalidation worker and the search path together never exceed two Turso
  requests per second. Turso bills and throttles per request; without one
  shared limiter each worker keeps its own idea of "politeness" and the sum of
  them is a request storm.
* `TursoHttpClient` — a small client for Turso's `/v2/pipeline` endpoint that
  checks every step. Turso answers HTTP 200 with a per-step error object, so a
  client that only looks at the status code reports success for a write that
  never happened.

A `LocalSqliteClient` with the same interface backs `file:` URLs and the test
suite, so the whole application can run without a network.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Protocol, Sequence

import httpx

from app.engine.config import Settings, get_settings

logger = logging.getLogger(__name__)

# A statement plus its bound arguments, in the shape both clients accept.
Statement = tuple[str, Sequence[Any]]


class LibsqlError(RuntimeError):
    """A statement the database refused.

    The message always carries the failing SQL, because "constraint failed"
    without the statement is not something an operator can act on.
    """


class DatabaseUnavailable(LibsqlError):
    """The database could not be reached at all."""


@dataclass
class ResultSet:
    """One statement's outcome."""

    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    rows_affected: int = 0

    def first(self) -> list[Any] | None:
        return self.rows[0] if self.rows else None

    def scalar(self, default: Any = None) -> Any:
        row = self.first()
        if row is None or not row:
            return default
        return row[0]

    def dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


class LibsqlClient(Protocol):
    """What the rest of the application is allowed to assume about a database."""

    async def execute(self, sql: str, args: Sequence[Any] = ()) -> ResultSet: ...

    async def batch(self, statements: Sequence[Statement]) -> list[ResultSet]: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class ProcessRateLimiter:
    """Serialises every database call to at most `1 / interval` per second.

    A single lock and a single timestamp, shared by everything that touches the
    database, so the limit is a property of the process rather than of each
    caller. `interval_seconds=0.5` is the two-requests-per-second ceiling.
    """

    def __init__(self, interval_seconds: float = 0.5) -> None:
        if interval_seconds < 0:
            raise ValueError("interval_seconds must not be negative")
        self.interval_seconds = float(interval_seconds)
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._calls = 0

    @property
    def calls(self) -> int:
        """How many calls have passed through this limiter."""
        return self._calls

    async def acquire(self) -> None:
        """Wait until the next call is allowed, then claim the slot."""
        async with self._lock:
            now = time.monotonic()
            if self._last_call:
                wait_for = self.interval_seconds - (now - self._last_call)
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
                    now = time.monotonic()
            self._last_call = now
            self._calls += 1

    # `wait` is the name used at the call sites; keep both spellings working.
    async def wait(self) -> None:
        await self.acquire()


# ---------------------------------------------------------------------------
# Turso over HTTP
# ---------------------------------------------------------------------------

_NULL = {"type": "null"}


#: How long to wait for Turso to answer one pipeline request. A cold
#: connection running a multi-statement pipeline can exceed the old 20s.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0


def retry_after_seconds(
    response: Any, default: float = 1.0, cap: float = 10.0
) -> float:
    """How long to wait after a 429, from `Retry-After` when it is a number."""
    raw = ""
    headers = getattr(response, "headers", None)
    if headers:
        raw = headers.get("Retry-After") or headers.get("retry-after") or ""
    try:
        value = float(raw) if raw else default
    except (TypeError, ValueError):
        value = default  # an HTTP-date form; the default is close enough
    return max(0.0, min(value, cap))


def encode_arg(value: Any) -> dict[str, Any]:
    """Encode a Python value as a Hrana typed value.

    The wire format is asymmetric and getting it wrong is fatal: an `integer`
    carries a **decimal string** (`{"type": "integer", "value": "42"}`) while
    a `float` carries a **JSON number** (`{"type": "float", "value": 0.999}`).
    Sending a float as `"0.999"` makes Turso reject the whole pipeline with
    `JSON parse error: invalid type: string "0.999", expected f64`, which is
    what happened to every `quality_score` write before this was fixed.
    """
    if value is None:
        return dict(_NULL)
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": float(value)}
    if isinstance(value, Decimal):
        return {"type": "float", "value": float(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode("ascii")}
    return {"type": "text", "value": str(value)}


def decode_cell(cell: Any) -> Any:
    """Decode a Hrana typed value into a Python value."""
    if not isinstance(cell, dict):
        return cell
    kind = cell.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        raw = cell.get("value")
        return int(raw) if raw is not None else None
    if kind == "float":
        raw = cell.get("value")
        return float(raw) if raw is not None else None
    if kind == "blob":
        raw = cell.get("base64") or ""
        return base64.b64decode(raw)
    if kind == "text":
        return cell.get("value")
    return cell.get("value")


def rewrite_scheme(url: str) -> str:
    """Map libSQL schemes onto the HTTP client Turso actually accepts.

    `libsql://`, `ws://` and `wss://` all select a WebSocket backend in most
    clients, and Turso's Hrana endpoint rejects those with a 400. The pipeline
    endpoint is plain HTTPS.
    """
    url = url.strip()
    for prefix, replacement in (
        ("libsql://", "https://"),
        ("wss://", "https://"),
        ("ws://", "http://"),
    ):
        if url.startswith(prefix):
            return replacement + url[len(prefix) :]
    return url


class TursoHttpClient:
    """A minimal, strict client for Turso's Hrana-over-HTTP pipeline."""

    def __init__(
        self,
        url: str,
        token: str = "",
        *,
        limiter: ProcessRateLimiter | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not url:
            raise ValueError("a Turso database URL is required")
        self.base_url = rewrite_scheme(url).rstrip("/")
        self.token = token
        self.limiter = limiter or ProcessRateLimiter(0.5)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers=self._headers(),
        )

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        return headers

    async def execute(self, sql: str, args: Sequence[Any] = ()) -> ResultSet:
        results = await self.batch([(sql, args)])
        return results[0]

    async def batch(self, statements: Sequence[Statement]) -> list[ResultSet]:
        """Run statements in order in one request and check every one of them."""
        if not statements:
            return []

        payload = {
            "requests": [
                {
                    "type": "execute",
                    "stmt": {
                        "sql": sql,
                        "args": [encode_arg(value) for value in args],
                    },
                    "want_rows": True,
                }
                for sql, args in statements
            ]
        }

        await self.limiter.acquire()
        try:
            response = await self._client.post(f"{self.base_url}/v2/pipeline", json=payload)
        except httpx.HTTPError as error:
            raise DatabaseUnavailable(f"Turso request failed: {error}") from error

        # A 429 is throttling, not a failure. Without this, every request Turso
        # rate-limits fails the whole batch and the crawl makes no progress
        # until the window resets — which at the request rates this crawler now
        # uses would look exactly like a stall. One bounded retry, honouring
        # Retry-After, turns that into a pause.
        if response.status_code == 429:
            delay = retry_after_seconds(response)
            logger.warning("Turso throttled the pipeline (429); retrying in %.1fs", delay)
            await asyncio.sleep(delay)
            await self.limiter.acquire()
            try:
                response = await self._client.post(
                    f"{self.base_url}/v2/pipeline", json=payload
                )
            except httpx.HTTPError as error:
                raise DatabaseUnavailable(f"Turso request failed: {error}") from error

        if response.status_code >= 400:
            raise DatabaseUnavailable(
                f"Turso answered HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            body = response.json()
        except ValueError as error:
            raise DatabaseUnavailable("Turso returned a non-JSON body") from error

        if "error" in body and body["error"]:
            raise LibsqlError(f"Turso rejected the pipeline: {body['error']}")

        steps = body.get("results") or []
        if len(steps) != len(statements):
            raise LibsqlError(
                f"Turso returned {len(steps)} results for {len(statements)} statements"
            )

        results: list[ResultSet] = []
        for index, (step, (sql, _args)) in enumerate(zip(steps, statements)):
            results.append(self._decode_step(step, index, sql))
        return results

    @staticmethod
    def _decode_step(step: dict[str, Any], index: int, sql: str) -> ResultSet:
        if step.get("type") == "error":
            error = step.get("error") or {}
            raise LibsqlError(
                f"libsql step {index} failed: {error.get('message', 'unknown error')} "
                f"(statement: {sql})"
            )

        response = step.get("response") or {}
        if response.get("type") == "error":
            error = response.get("error") or {}
            raise LibsqlError(
                f"libsql step {index} failed: {error.get('message', 'unknown error')} "
                f"(statement: {sql})"
            )

        result = response.get("result") or {}
        columns = [column.get("name", "") for column in result.get("cols") or []]
        rows = [[decode_cell(cell) for cell in row] for row in result.get("rows") or []]
        affected = int(result.get("affected_row_count") or 0)
        return ResultSet(columns=columns, rows=rows, rows_affected=affected)

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Local SQLite
# ---------------------------------------------------------------------------


class LocalSqliteClient:
    """The same interface over the standard library, for local runs and tests."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = None
        self._lock = asyncio.Lock()
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")

    async def execute(self, sql: str, args: Sequence[Any] = ()) -> ResultSet:
        results = await self.batch([(sql, args)])
        return results[0]

    async def batch(self, statements: Sequence[Statement]) -> list[ResultSet]:
        if not statements:
            return []
        async with self._lock:
            return await asyncio.to_thread(self._run, list(statements))

    def _run(self, statements: list[Statement]) -> list[ResultSet]:
        results: list[ResultSet] = []
        for sql, args in statements:
            try:
                cursor = self._connection.execute(sql, tuple(args))
                columns = [description[0] for description in cursor.description or []]
                rows = [list(row) for row in cursor.fetchall()] if cursor.description else []
                results.append(
                    ResultSet(
                        columns=columns,
                        rows=rows,
                        rows_affected=cursor.rowcount if cursor.rowcount > 0 else 0,
                    )
                )
            except sqlite3.Error as error:
                raise LibsqlError(f"sqlite failed: {error} (statement: {sql})") from error
        return results

    async def close(self) -> None:
        await asyncio.to_thread(self._connection.close)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_client(settings: Settings | None = None) -> LibsqlClient:
    """Build the client the configured URL asks for, or refuse to guess."""
    settings = settings or get_settings()
    url = settings.database_url

    if not url or url.startswith("file:"):
        if settings.is_production and not settings.allow_local_db:
            raise DatabaseUnavailable(
                "refusing to start: no remote database configured. Set "
                "TURSO_DB_URL and TURSO_AUTH_TOKEN, or set ALLOW_LOCAL_DB=true "
                "if a local file is genuinely intended."
            )
        path = url[len("file:") :] if url.startswith("file:") else settings.local_db_path
        path = path or ":memory:"
        logger.warning(
            "Connected to LOCAL SQLite: %s — this is not the Turso cloud database", path
        )
        return LocalSqliteClient(path)

    if not settings.turso_auth_token:
        raise DatabaseUnavailable(
            f"TURSO_AUTH_TOKEN is required for the remote database {url}"
        )

    logger.info("Connected to Remote Turso DB: %s (HttpClient)", rewrite_scheme(url))
    return TursoHttpClient(
        url,
        settings.turso_auth_token,
        limiter=ProcessRateLimiter(settings.libsql_min_interval_seconds),
    )


def statements_from_rows(rows: Iterable[Sequence[Any]]) -> list[Statement]:
    """Small helper for callers that build statements dynamically."""
    return [(row[0], row[1:]) for row in rows]


__all__ = [
    "DatabaseUnavailable",
    "LibsqlClient",
    "LibsqlError",
    "LocalSqliteClient",
    "ProcessRateLimiter",
    "ResultSet",
    "Statement",
    "TursoHttpClient",
    "build_client",
    "decode_cell",
    "encode_arg",
    "retry_after_seconds",
    "rewrite_scheme",
]
