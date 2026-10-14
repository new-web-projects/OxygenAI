"""
Rate limiting — Passage 4 §6.2 (G08b): "Rate limit: per-plan, e.g.
30/min" on POST /api/ai/analyze, with the broader per-plan concept
extending to every route.

In-process token buckets. This is the same documented limitation as the
circuit breaker (`providers/circuit_breaker.py`): correct and sufficient
for a single API instance; a multi-instance deployment needs this state
in Redis (Passage 1 §3 lists Redis for exactly this — the Redis phase of
the implementation plan swaps the bucket storage behind this middleware).

Bucket identity (Phase 1 hardening; each defect reproduced before fixing):

* Authenticated requests are keyed per USER (validly signed, unexpired
  bearer token — signature/expiry only, no database hit; revocation is the
  auth dependency's job). "Per-plan" limits therefore follow the account,
  not the network path.
* Anonymous requests are keyed by `security.network.client_ip`, which
  honours X-Forwarded-For only for `TRUSTED_PROXY_COUNT` proxies. The old
  key was the first X-Forwarded-For value, which any client can rotate —
  8 of 8 requests passed a 5/min limit that way.
* Credential endpoints (login/register) have their own tighter bucket
  (`RATE_LIMIT_AUTH`); the provider `/test` and `/health` probes (which
  spend provider tokens) now land in the admin bucket — the old prefix
  match only covered `/api/ai/models` and `/api/ai/route`.
* Idle buckets are pruned, so the table no longer gains one entry per
  distinct client for the life of the process.

A 429 carries a `Retry-After` header and the standard error body, so a
rate-limited client can back off correctly rather than hammering the
endpoint.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import RateLimitSettings, get_settings
from .errors import error_body
from .observability import get_logger
from .security.jwt import TokenError, decode_access_token
from .security.network import client_ip

logger = get_logger("rate_limit")

_PROVIDER_PROBE = re.compile(r"^/api/ai/[^/]+/(test|health)$")
_PRUNE_INTERVAL_SECONDS = 300.0
_IDLE_SECONDS = 600.0


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
    if path in ("/api/auth/login", "/api/auth/register"):
        return "auth", settings.auth_per_minute
    if path.startswith("/api/ai/analyze"):
        return "analyze", settings.analyze_per_minute
    if (
        path.startswith("/api/ai/models")
        or path.startswith("/api/ai/route")
        or _PROVIDER_PROBE.match(path)
    ):
        return "admin", settings.admin_per_minute
    return "default", settings.default_per_minute


def _client_key(request: Request) -> str:
    """`user:<id>` for a valid bearer token, otherwise `ip:<addr>`."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        try:
            return f"user:{decode_access_token(token.strip()).user_id}"
        except TokenError:
            pass  # a garbage token buys no extra identity — fall back to the IP
    return f"ip:{client_ip(request) or 'unknown'}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: RateLimitSettings | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._settings = settings or get_settings().rate_limit
        self._buckets: dict[tuple[str, str], _TokenBucket] = {}
        self._last_prune = time.monotonic()

    def _prune(self, now: float) -> None:
        if now - self._last_prune < _PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        stale = [k for k, b in self._buckets.items() if now - b.last_refill > _IDLE_SECONDS]
        for key in stale:
            del self._buckets[key]

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
        now = time.monotonic()
        self._prune(now)
        bucket = self._bucket_for(client_key, name, per_minute)
        allowed, retry_after = bucket.try_consume(now)

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