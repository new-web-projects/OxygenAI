"""
Observability: structured logging, request ids, and request timing.

Replaces the three bare `print()` calls that were the entire
error-reporting story in `routers/analyze.py`. Passage 4 §7.2's
concern-mapping table places structured logging per-service rather than
in a central folder, which is what this module is — the API service's
own logging wiring.

A request id is attached to every request and echoed back as the
`X-Request-Id` header, so a user-visible failure can be traced to a log
line without guessing.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "requestId": request_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<8} [{request_id_var.get()}] {record.name}: {record.getMessage()}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    root = logging.getLogger()
    # Replace any handler a previous configure call installed, so
    # --reload doesn't stack duplicates.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("uvicorn.access").propagate = False


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, times the request, logs the outcome."""

    def __init__(self, app: Any, logger_name: str = "oxygen.request") -> None:
        super().__init__(app)
        self.logger = logging.getLogger(logger_name)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id")
        request_id = incoming or uuid.uuid4().hex[:12]
        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            self.logger.exception(
                "request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "latencyMs": elapsed_ms,
                },
            )
            raise
        finally:
            request_id_var.reset(token)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Response-Time-Ms"] = str(elapsed_ms)
        log = self.logger.info if response.status_code < 500 else self.logger.error
        log(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "latencyMs": elapsed_ms,
            },
        )
        return response


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"oxygen.{name}")