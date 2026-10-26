"""Error types and the single error envelope both endpoints share."""

from __future__ import annotations

from typing import Any, Literal

ErrorCode = Literal[
    "bad_request",
    "not_found",
    "rate_limited",
    "provider_unavailable",
    "database_error",
    "internal",
]


class AppError(Exception):
    """Base class for errors that are safe to show a client."""

    code: ErrorCode = "internal"
    status_code: int = 500
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        if retryable is not None:
            self.retryable = retryable

    def envelope(self) -> dict[str, Any]:
        return error_envelope(self.code, self.message, self.retryable)


class BadRequestError(AppError):
    code: ErrorCode = "bad_request"
    status_code: int = 400
    retryable: bool = False


class NotFoundError(AppError):
    code: ErrorCode = "not_found"
    status_code: int = 404
    retryable: bool = False


class RateLimitedError(AppError):
    code: ErrorCode = "rate_limited"
    status_code: int = 429
    retryable: bool = True


class ProviderError(AppError):
    """A retrieval provider failed, timed out, or returned unparseable data."""

    code: ErrorCode = "provider_unavailable"
    status_code: int = 503
    retryable: bool = True

    def __init__(self, message: str, *, provider: str = "unknown") -> None:
        super().__init__(message)
        self.provider = provider


class DatabaseError(AppError):
    code: ErrorCode = "database_error"
    status_code: int = 503
    retryable: bool = True


def error_envelope(code: ErrorCode, message: str, retryable: bool) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "retryable": retryable}}
