"""A PostgreSQL client behind the same interface the rest of the app uses.

The application talks to `LibsqlClient`: `execute(sql, args)`,
`batch(statements)` and `close()`, returning `ResultSet` objects. Keeping that
shape means the store, the guard, the indexer and the orchestrator do not have
to know which database is underneath — only the SQL they send is dialect
specific, and that lives in one place per query.

Two translation details matter and both are easy to get wrong:

* **Placeholders.** The code writes `?`. psycopg wants `%s`. But a literal `%`
  in the SQL — `LIKE '%.gov.in'` — is also special to psycopg's argument
  binding, so it must be doubled. Splitting on `?` and escaping the literal
  halves does both correctly in one pass; a naive `str.replace("?", "%s")`
  leaves every LIKE pattern broken.
* **One transaction per batch.** The SQLite client ran a batch as a unit, and
  callers rely on that: a half-applied batch leaves the queue and the page rows
  disagreeing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

from app.libsql_http import DatabaseUnavailable, LibsqlError, ResultSet, Statement

logger = logging.getLogger(__name__)

__all__ = ["PostgresClient", "is_transaction_control", "to_pg_placeholders"]


#: Transaction control the store issues for SQLite. PostgreSQL rejects
#: `BEGIN IMMEDIATE`, and this client already wraps a batch in a transaction, so
#: these are dropped rather than forwarded.
_TRANSACTION_CONTROL = frozenset(
    {"BEGIN", "BEGIN IMMEDIATE", "BEGIN TRANSACTION", "COMMIT", "END", "ROLLBACK"}
)


def is_transaction_control(sql: str) -> bool:
    return " ".join(sql.strip().split()).rstrip(";").upper() in _TRANSACTION_CONTROL


def to_pg_placeholders(sql: str) -> str:
    """Rewrite `?` placeholders for psycopg, escaping literal percent signs."""
    if "?" not in sql:
        return sql.replace("%", "%%")
    return "%s".join(part.replace("%", "%%") for part in sql.split("?"))


class PostgresClient:
    """The `LibsqlClient` interface over psycopg 3."""

    dialect = "postgres"

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        statement_timeout_ms: int = 4000,
        acquire_timeout_seconds: float = 2.0,
    ) -> None:
        if not dsn:
            raise ValueError("a PostgreSQL DSN is required")
        self.dsn = dsn
        self._min_size = max(1, min_size)
        self._max_size = max(self._min_size, max_size)
        #: A search must answer or fail; it must never sit behind the crawler's
        #: writes until the client gives up. PostgreSQL aborts the statement at
        #: this many milliseconds and the caller gets a clear error instead of a
        #: hung request.
        self.statement_timeout_ms = max(0, int(statement_timeout_ms))
        #: How long to wait for a pooled connection before giving up. Without
        #: this, a saturated pool blocks indefinitely.
        self.acquire_timeout_seconds = max(0.1, float(acquire_timeout_seconds))
        self._pool: Any = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def _connect(self) -> Any:
        try:
            from psycopg_pool import AsyncConnectionPool
        except ImportError as error:  # pragma: no cover - dependency error
            raise DatabaseUnavailable(
                "psycopg_pool is required for PostgreSQL: pip install 'psycopg[binary,pool]'"
            ) from error

        pool = AsyncConnectionPool(
            self.dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            open=False,
            kwargs={"autocommit": False},
        )
        await pool.open(wait=True, timeout=20)
        return pool

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    self._pool = await self._connect()
        return self._pool

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    # -- the LibsqlClient surface -----------------------------------------

    async def execute(self, sql: str, args: Sequence[Any] = ()) -> ResultSet:
        results = await self.batch([(sql, args)])
        return results[0]

    async def batch(self, statements: Sequence[Statement]) -> list[ResultSet]:
        if not statements:
            return []

        pool = await self._ensure_pool()
        results: list[ResultSet] = []
        try:
            async with pool.connection(timeout=self.acquire_timeout_seconds) as connection:
                # One transaction for the whole batch, as the SQLite client did.
                async with connection.transaction():
                    if self.statement_timeout_ms:
                        # SET LOCAL is scoped to this transaction, so a slow
                        # query cannot poison the connection for the next caller.
                        await self._run(
                            connection,
                            f"SET LOCAL statement_timeout = {self.statement_timeout_ms}",
                            (),
                        )
                    for sql, args in statements:
                        if is_transaction_control(sql):
                            # The surrounding transaction already provides it.
                            results.append(ResultSet())
                            continue
                        results.append(await self._run(connection, sql, args))
        except Exception as error:
            raise self._translate(error) from error
        return results

    async def _run(self, connection: Any, sql: str, args: Sequence[Any]) -> ResultSet:
        async with connection.cursor() as cursor:
            await cursor.execute(to_pg_placeholders(sql), tuple(args))
            columns = [column.name for column in (cursor.description or [])]
            rows: list[list[Any]] = []
            if cursor.description is not None:
                rows = [list(row) for row in await cursor.fetchall()]
            return ResultSet(
                columns=columns,
                rows=rows,
                rows_affected=max(0, cursor.rowcount or 0),
            )

    # -- errors ------------------------------------------------------------

    @staticmethod
    def _translate(error: Exception) -> Exception:
        """Map driver errors onto the app's own, so callers keep working."""
        if isinstance(error, (DatabaseUnavailable, LibsqlError)):
            return error
        try:
            import psycopg
        except ImportError:  # pragma: no cover
            return DatabaseUnavailable(f"PostgreSQL request failed: {error}")

        # A statement timeout cancels the query but leaves the connection
        # healthy, so it must not be reported as a connection failure — the
        # caller would retry a database that is answering perfectly well.
        if isinstance(error, getattr(psycopg.errors, "QueryCanceled", ())):
            return LibsqlError(f"postgres statement timed out: {error}")
        if isinstance(error, psycopg.OperationalError):
            return DatabaseUnavailable(f"PostgreSQL connection failed: {error}")
        if isinstance(error, psycopg.Error):
            return LibsqlError(f"postgres failed: {error}")
        return DatabaseUnavailable(f"PostgreSQL request failed: {error}")
