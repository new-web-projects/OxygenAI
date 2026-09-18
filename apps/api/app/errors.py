"""
One error shape for the whole API.

Defect this fixes, confirmed live before this pass: `main.py` reshaped
only `RequestValidationError` into `{"error", "details"}`, while every
`HTTPException` fell through to FastAPI's default `{"detail": ...}`. The
frontend reads `data.error` only, so every genuinely useful message —
"providers must not repeat", "Ratings require a database", a 404, a
provider failure — rendered in the UI as the generic "Request failed".

A second confirmed defect is closed here too: a malformed comparison id
reached asyncpg as a raw string and raised `DataError`, escaping as a
500 with a full traceback in the response. `DataError` and friends are
now mapped to a clean 400.

Passage 4 §6.2 (G08b) specifies the error semantics this module carries:
400 invalid input · 401/403 auth · 404 not found · 409 market data
unavailable · 429 rate limited · 503 provider unavailable.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("oxygen.errors")


class ApiError(Exception):
    """
    Application-level error carrying the full contract shape. Routers
    raise this (or a subclass) instead of HTTPException so that status,
    machine-readable code, human message, and structured details all
    travel together.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        self.details = details


class ValidationError(ApiError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "invalid_request"


class NotFoundError(ApiError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class UnauthorizedError(ApiError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ForbiddenError(ApiError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class RateLimitedError(ApiError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class MarketDataUnavailableError(ApiError):
    """Passage 4 §6.2: 409 — the engine returns NO_VALID_SETUP rather than an HTTP error."""

    status_code = status.HTTP_409_CONFLICT
    code = "market_data_unavailable"


class ProviderUnavailableError(ApiError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "provider_unavailable"


class ConfigurationError(ApiError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "not_configured"


def error_body(
    message: str, code: str, details: Any = None, error_id: str | None = None
) -> dict[str, Any]:
    """
    The single response body every failure uses.

    `error` is kept as the human-readable message because the existing
    frontend already reads exactly that key — preserving it means the
    fix requires no frontend contract change, only that every path now
    populates it. `detail` is mirrored so any client written against
    FastAPI's default shape keeps working.
    """
    body: dict[str, Any] = {"error": message, "code": code, "detail": message}
    if details is not None:
        body["details"] = details
    if error_id is not None:
        body["errorId"] = error_id
    return body


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.message, exc.code, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Same {"error", "details"} shape as before this pass, so existing
        # clients are unaffected; `code` is additive.
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=error_body("Invalid request", "invalid_request", _safe_details(exc.errors())),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        code = _code_for_status(exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(detail, code),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Driver-level input errors (e.g. a malformed UUID handed to
        # asyncpg) are a client mistake, not a server fault. Detected by
        # class name so this module carries no asyncpg import.
        if type(exc).__name__ in ("DataError", "InvalidTextRepresentationError"):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=error_body(
                    "One or more identifiers in the request are malformed.", "invalid_request"
                ),
            )
        error_id = uuid.uuid4().hex[:12]
        logger.exception(
            "unhandled error", extra={"error_id": error_id, "path": str(request.url.path)}
        )
        # The traceback goes to the log, never to the client.
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body(
                "An unexpected error occurred. Quote the error id when reporting it.",
                "internal_error",
                error_id=error_id,
            ),
        )


def _safe_details(errors: Any) -> Any:
    """
    Pydantic error dicts can carry a non-serialisable `ctx.error`
    (an exception instance). Strip anything that won't encode.
    """
    cleaned = []
    try:
        for item in errors:
            entry = {k: v for k, v in item.items() if k != "ctx"}
            ctx = item.get("ctx")
            if isinstance(ctx, dict):
                entry["ctx"] = {k: str(v) if isinstance(v, BaseException) else v for k, v in ctx.items()}
            cleaned.append(entry)
    except Exception:  # noqa: BLE001 — details are diagnostic, never load-bearing
        return None
    return cleaned


def _code_for_status(status_code: int) -> str:
    return {
        400: "invalid_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "invalid_request",
        429: "rate_limited",
        503: "service_unavailable",
    }.get(status_code, "error")