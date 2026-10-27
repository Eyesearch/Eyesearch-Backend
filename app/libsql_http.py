"""A Turso/LibSQL HTTP client with parsing we control.

`libsql_client`'s HTTP backend indexes the parsed JSON with `response["result"]`.
Turso answers the pipeline endpoint with `{"results": [...]}` — a list of step
objects — so that index raises `KeyError: 'result'` and every read and write to
`crawl_backlog` silently returned nothing.

This module speaks the v2 pipeline protocol directly and normalises every shape
we have seen in the wild:

    {"results": [{"type": "ok", "response": {"type": "execute", "result": {...}}}]}
    {"result": {"cols": [...], "rows": [...]}}          # single execute
    {"results": [{"cols": [...], "rows": [...]}]}       # plain list
    {"type": "ok", "response": {"type": "execute", "result": {...}}}   # one step

Rows arrive as `{"type": "text", "value": "..."}` cells, which are decoded to
Python values. A statement with no rows (INSERT, UPDATE) simply yields an empty
result — success is not an error.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import httpx

from app.errors import DatabaseError

logger = logging.getLogger(__name__)


class ProcessRateLimiter:
    """Process-wide minimum interval between outbound HTTP request starts."""

    def __init__(self, interval_seconds: float = 0.5) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.interval_seconds = float(interval_seconds)
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            delay = self._next_start - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_start = time.monotonic() + self.interval_seconds


_HTTP_RATE_LIMITER = ProcessRateLimiter(interval_seconds=0.5)


def _iter_pipeline_errors(payload: Any):
    """Yield nested Hrana step errors, including errors inside batch results."""
    if isinstance(payload, dict):
        if payload.get("type") == "error":
            yield payload
            return
        for value in payload.values():
            yield from _iter_pipeline_errors(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            yield from _iter_pipeline_errors(value)


@dataclass(slots=True)
class ResultSet:
    """The shape the rest of the app reads: `.rows` and `.columns`."""

    rows: tuple[tuple[Any, ...], ...] = ()
    columns: tuple[str, ...] = ()


def _decode_value(cell: Any) -> Any:
    """Decode one Hrana/Turso cell into a Python value."""
    if cell is None:
        return None
    if isinstance(cell, dict):
        kind = cell.get("type")
        if kind == "null":
            return None
        if kind == "blob":
            import base64

            raw = cell.get("base64")
            return base64.b64decode(raw) if raw else None
        value = cell.get("value")
        if kind == "integer":
            try:
                return int(value)
            except (TypeError, ValueError):
                return value
        if kind == "float":
            try:
                return float(value)
            except (TypeError, ValueError):
                return value
        return value
    if isinstance(cell, (list, tuple)):
        return tuple(_decode_value(item) for item in cell)
    return cell


def _columns_of(payload: dict[str, Any]) -> tuple[str, ...]:
    cols = payload.get("cols") or payload.get("columns") or ()
    names: list[str] = []
    for col in cols:
        if isinstance(col, dict):
            names.append(str(col.get("name") or ""))
        else:
            names.append(str(col))
    return tuple(names)


def _rows_of(payload: dict[str, Any]) -> tuple[tuple[Any, ...], ...]:
    rows = payload.get("rows") or ()
    decoded: list[tuple[Any, ...]] = []
    for row in rows:
        if isinstance(row, dict) and "value" in row and "type" in row:
            decoded.append((_decode_value(row),))
        elif isinstance(row, (list, tuple)):
            decoded.append(tuple(_decode_value(cell) for cell in row))
        else:
            decoded.append((_decode_value(row),))
    return tuple(decoded)


def _result_from_payload(payload: Any) -> ResultSet:
    """Best-effort extraction from any response shape we have seen."""
    if payload is None:
        return ResultSet()
    if isinstance(payload, ResultSet):
        return payload
    if not isinstance(payload, dict):
        return ResultSet()

    # a step object: {"type": "ok", "response": {...}}
    if "response" in payload and isinstance(payload["response"], dict):
        return _result_from_payload(payload["response"])

    # a step that failed
    if payload.get("type") == "error":
        error = payload.get("error") or {}
        raise DatabaseError(f"libsql step failed: {error.get('message') or error}")

    # a container holding one or more results
    if "results" in payload:
        results = payload["results"]
        if isinstance(results, list):
            first = results[0] if results else None
            if first is None:
                return ResultSet()
            merged_rows: list[tuple[Any, ...]] = []
            columns: tuple[str, ...] = ()
            for step in results:
                extracted = _result_from_payload(step)
                if extracted.columns and not columns:
                    columns = extracted.columns
                merged_rows.extend(extracted.rows)
            return ResultSet(rows=tuple(merged_rows), columns=columns)
        return _result_from_payload(results)

    # the actual execute result
    if "result" in payload:
        return _result_from_payload(payload["result"])

    if "cols" in payload or "columns" in payload or "rows" in payload:
        return ResultSet(rows=_rows_of(payload), columns=_columns_of(payload))

    return ResultSet()


def parse_pipeline_response(payload: Any) -> list[ResultSet]:
    """Parse a v2 pipeline response into one ResultSet per request."""
    if not isinstance(payload, dict):
        return [ResultSet()]
    steps = payload.get("results")
    if not isinstance(steps, list):
        return [_result_from_payload(payload)]
    if not steps:
        return [ResultSet()]  # an empty pipeline is "no rows", not an error
    return [_result_from_payload(step) for step in steps]


def _encode_arg(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray)):
        import base64

        return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode()}
    return {"type": "text", "value": str(value)}


class TursoHTTPClient:
    """Minimal async client for the Turso HTTP v2 pipeline."""

    def __init__(
        self,
        url: str,
        *,
        auth_token: str | None = None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base = url.rstrip("/")
        for suffix in ("/v2/pipeline", "/v1/pipeline", "/v2", "/v1"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        self._base = base
        self._token = auth_token
        self._timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self.last_errors: list[str] = []

    @property
    def endpoint(self) -> str:
        return f"{self._base}/v2/pipeline"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # Statement-type counters, so "hundreds of POSTs, no writes" is answerable
    # from the log instead of by inference.
    _COUNTS: dict[str, int] = {}
    _TOTAL = 0
    _strict: bool = True
    last_errors: list[str]

    @classmethod
    def _record(cls, requests: list[dict[str, Any]]) -> None:
        heads: list[str] = []
        for request in requests:
            sql = str(request.get("stmt", {}).get("sql", "")).strip()
            verb = sql.split()[0].upper() if sql else "?"
            cls._COUNTS[verb] = cls._COUNTS.get(verb, 0) + 1
            cls._TOTAL += 1
            heads.append(sql.splitlines()[0][:60])
        logger.debug("pipeline execute: %s", heads)
        if cls._TOTAL % 50 < len(requests):
            logger.debug("statements so far: %s", dict(sorted(cls._COUNTS.items())))

    async def _post(self, requests: list[dict[str, Any]]) -> list[ResultSet]:
        self._record(requests)
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, transport=self._transport)
        try:
            await _HTTP_RATE_LIMITER.acquire()
            response = await self._client.post(
                self.endpoint, json={"requests": requests}, headers=self._headers()
            )
        except Exception as exc:
            raise DatabaseError(f"libsql http request failed: {exc}") from exc

        if response.status_code >= 400:
            body = response.text[:300]
            raise DatabaseError(f"libsql http {response.status_code}: {body}")

        try:
            payload = response.json()
        except Exception as exc:
            raise DatabaseError(f"libsql http returned non-JSON: {response.text[:200]}") from exc

        self.last_errors = []
        steps = payload.get("results") if isinstance(payload, dict) else None
        # Turso answers 200 OK even when a statement inside the batch failed, so
        # every step has to be inspected. This is the difference between "the
        # request succeeded" and "the row was written".
        if isinstance(steps, list):
            for index, step in enumerate(steps):
                for failed_step in _iter_pipeline_errors(step):
                    error = failed_step.get("error") or {}
                    sql = ""
                    if index < len(requests):
                        request = requests[index]
                        stmt = request.get("stmt", {})
                        if not stmt and isinstance(request.get("batch"), dict):
                            batch_steps = request["batch"].get("steps") or []
                            if batch_steps:
                                stmt = batch_steps[0].get("stmt", {})
                        sql = str(stmt.get("sql", "")).strip().splitlines()[0][:120] if stmt else ""
                    message = (
                        f"libsql step {index} failed: {error.get('message') or error} "
                        f"(statement: {sql})"
                    )
                    self.last_errors.append(message)
                    if getattr(self, "_strict", True):
                        raise DatabaseError(message)
                    logger.error("%s", message)
        return parse_pipeline_response(payload)

    async def execute(self, sql: str, params: Sequence[Any] | None = None) -> ResultSet:
        requests = [
            {
                "type": "execute",
                "stmt": {
                    "sql": sql,
                    "args": [_encode_arg(value) for value in (params or [])],
                },
            }
        ]
        results = await self._post(requests)
        return results[0] if results else ResultSet()

    async def batch(
        self,
        statements: Iterable[Any],
        *,
        strict: bool = True,
    ) -> list[ResultSet]:
        """Run `[(sql, params), ...]` in one pipeline round trip.

        With `strict=False` a failing step does not discard the whole response:
        the successful steps are still returned and the caller decides what to do
        with the errors (see `last_errors`). Each `execute` step is its own
        implicit transaction, so a later failure cannot undo an earlier commit —
        but a caller that throws the response away would never know that.
        """
        requests: list[dict[str, Any]] = []
        for statement in statements:
            if isinstance(statement, (list, tuple)) and len(statement) == 2:
                sql, params = statement
            else:
                sql, params = statement, []
            requests.append(
                {
                    "type": "execute",
                    "stmt": {
                        "sql": str(sql),
                        "args": [_encode_arg(value) for value in (params or [])],
                    },
                }
            )
        if not requests:
            return []
        self._strict = strict
        try:
            return await self._post(requests)
        finally:
            self._strict = True

    async def batch_atomic(self, statements: Iterable[Any]) -> list[ResultSet]:
        """Execute statements in one Hrana write batch; raise on any failed step."""
        steps: list[dict[str, Any]] = []
        for statement in statements:
            if isinstance(statement, (list, tuple)) and len(statement) == 2:
                sql, params = statement
            else:
                sql, params = statement, []
            steps.append({
                "stmt": {
                    "sql": str(sql),
                    "args": [_encode_arg(value) for value in (params or [])],
                }
            })
        if not steps:
            return []
        request = {
            "type": "batch",
            "batch": {"steps": steps, "mode": "write"},
        }
        previous_strict = self._strict
        self._strict = True
        try:
            return await self._post([request])
        finally:
            self._strict = previous_strict

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = [
    "ResultSet",
    "TursoHTTPClient",
    "ProcessRateLimiter",
    "parse_pipeline_response",
]
