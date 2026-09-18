"""
Rate limiting — Passage 4 §6.2 (G08b): "Rate limit: per-plan, e.g.
30/min" on POST /api/ai/analyze, with the broader per-plan concept
extending to every route.

In-process token buckets, keyed by client IP. This is the same
documented limitation as the circuit breaker (`providers/circuit_breaker.py`):
correct and sufficient for a single API instance; a multi-instance
deployment would need this state in Redis (Passage 1 §3 lists Redis for
exactly this). Flagged rather than silently assumed to scale.

A 429 carries a `Retry-After` header and the standard error body, so a
rate-limited client can back off correctly rather than hammering the
endpoint.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import RateLimitSettings, get_settings
from .errors import error_body
from .observability import get_logger

logger = get_logger("rate_limit")


class _TokenBucket:
    __slots__ = ("capacity", "tokens", "refill_per_second", "last_refill")

    def __init__(self, capacity: float, refill_per_second: float) -> None:
        self.capacity = capacity
        self.tokens = capacity
        self.refill_per_second = refill_per_second
        self.last_refill = time.monotonic()

    def try_consume(self, now: float) -> tuple[bool, float]:
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0.0
        deficit = 1.0 - self.tokens
        wait = deficit / self.refill_per_second if self.refill_per_second > 0 else 60.0
        return False, wait


def _limit_for_path(path: str, settings: RateLimitSettings) -> tuple[str, int]:
    """Returns (bucket_name, requests_per_minute) for a request path."""
    if path.startswith("/api/ai/analyze"):
        return "analyze", settings.analyze_per_minute
    if path.startswith("/api/ai/models") or path.startswith("/api/ai/route"):
        return "admin", settings.admin_per_minute
    return "default", settings.default_per_minute


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: RateLimitSettings | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._settings = settings or get_settings().rate_limit
        self._buckets: dict[tuple[str, str], _TokenBucket] = {}

    def _bucket_for(self, key: str, name: str, per_minute: int) -> _TokenBucket:
        bucket_key = (key, name)
        bucket = self._buckets.get(bucket_key)
        if bucket is None:
            bucket = _TokenBucket(capacity=float(per_minute), refill_per_second=per_minute / 60.0)
            self._buckets[bucket_key] = bucket
        return bucket

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self._settings.enabled or request.method == "OPTIONS":
            return await call_next(request)

        name, per_minute = _limit_for_path(request.url.path, self._settings)
        if per_minute <= 0:
            return await call_next(request)

        client_key = _client_key(request)
        bucket = self._bucket_for(client_key, name, per_minute)
        allowed, retry_after = bucket.try_consume(time.monotonic())

        if not allowed:
            logger.info(
                "rate limited",
                extra={"client": client_key, "bucket": name, "path": request.url.path},
            )
            return JSONResponse(
                status_code=429,
                content=error_body(
                    f"Too many requests — this endpoint allows {per_minute}/min. "
                    f"Retry in ~{retry_after:.0f}s.",
                    "rate_limited",
                ),
                headers={"Retry-After": str(max(1, round(retry_after)))},
            )
        return await call_next(request)