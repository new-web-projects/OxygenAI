"""
Gateway-layer security behaviour, tested against small purpose-built apps
(no database, no provider calls) so each property is exercised in
isolation and the tests run anywhere.

Each test pins a defect that was reproduced against the running app
before it was fixed (see the module docstrings of `rate_limit.py`,
`errors.py`, `observability.py`):

* rotating a spoofed X-Forwarded-For defeated every rate limit;
* unhandled-error (500) responses carried no CORS header, so a browser
  could not read the error id it is told to quote;
* the access-log line for every request carried requestId "-".
"""

from __future__ import annotations

import logging
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import RateLimitSettings, get_settings
from app.errors import install_error_handlers
from app.observability import RequestContextMiddleware, request_id_var
from app.rate_limit import RateLimitMiddleware
from app.security.jwt import create_access_token


@pytest.fixture
def env(monkeypatch):
    """Set env vars and refresh cached settings; restore both afterwards."""

    def _set(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        get_settings(refresh=True)

    yield _set
    monkeypatch.undo()
    get_settings(refresh=True)


def _limits(**overrides) -> RateLimitSettings:
    base = dict(enabled=True, analyze_per_minute=3, default_per_minute=100, admin_per_minute=100, auth_per_minute=100)
    base.update(overrides)
    return RateLimitSettings(**base)


def _rate_limited_app(settings: RateLimitSettings) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, settings=settings)

    @app.post("/api/ai/analyze")
    async def analyze():
        return {"ok": True}

    @app.post("/api/auth/login")
    async def login():
        return {"ok": True}

    @app.get("/api/ai/grok/health")
    async def probe():
        return {"ok": True}

    @app.get("/api/ai/providers")
    async def providers():
        return {"ok": True}

    return app


def _client(app: FastAPI, peer: str = "198.51.100.7") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(peer, 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _statuses(client: httpx.AsyncClient, n: int, headers_for=lambda i: {}, path="/api/ai/analyze", method="POST"):
    out = []
    for i in range(n):
        response = await client.request(method, path, headers=headers_for(i))
        out.append(response.status_code)
    return out


# --------------------------------------------------------------------------
# Rate-limit identity
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotating_a_spoofed_forwarded_for_header_cannot_bypass_the_limit(env):
    env(TRUSTED_PROXY_COUNT="0")
    async with _client(_rate_limited_app(_limits())) as client:
        codes = await _statuses(client, 6, headers_for=lambda i: {"X-Forwarded-For": f"198.51.100.{i + 10}"})
    # Before the fix this was [200]*6 — each new header value got a fresh bucket.
    assert codes == [200, 200, 200, 429, 429, 429]


@pytest.mark.asyncio
async def test_a_trusted_proxy_makes_forwarded_for_the_identity(env):
    env(TRUSTED_PROXY_COUNT="1")
    async with _client(_rate_limited_app(_limits())) as client:
        a = await _statuses(client, 4, headers_for=lambda i: {"X-Forwarded-For": "203.0.113.1"})
        b = await _statuses(client, 3, headers_for=lambda i: {"X-Forwarded-For": "203.0.113.2"})
    assert a == [200, 200, 200, 429]  # one real client, one bucket
    assert b == [200, 200, 200]  # a different real client is unaffected


@pytest.mark.asyncio
async def test_authenticated_requests_share_one_bucket_per_user_across_ips(env):
    env(JWT_SECRET="test-secret-do-not-use-in-production", JWT_ISSUER="oxygen-ai-test", TRUSTED_PROXY_COUNT="0")
    token, _ = create_access_token("11111111-1111-1111-1111-111111111111", "u@example.com", "user")
    headers = {"Authorization": f"Bearer {token}"}
    app = _rate_limited_app(_limits())
    async with _client(app, peer="198.51.100.1") as first, _client(app, peer="198.51.100.2") as second:
        codes = await _statuses(first, 2, headers_for=lambda i: headers)
        codes += await _statuses(second, 2, headers_for=lambda i: headers)
    # Same account from two networks draws on the SAME 3-request budget.
    assert codes == [200, 200, 200, 429]


@pytest.mark.asyncio
async def test_a_garbage_token_buys_no_extra_identity(env):
    env(JWT_SECRET="test-secret-do-not-use-in-production", JWT_ISSUER="oxygen-ai-test", TRUSTED_PROXY_COUNT="0")
    async with _client(_rate_limited_app(_limits())) as client:
        codes = await _statuses(
            client, 5, headers_for=lambda i: {"Authorization": f"Bearer not-a-real-token-{i}"}
        )
    assert codes == [200, 200, 200, 429, 429]


# --------------------------------------------------------------------------
# Bucket selection
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_endpoints_use_the_tight_auth_bucket(env):
    env(TRUSTED_PROXY_COUNT="0")
    async with _client(_rate_limited_app(_limits(auth_per_minute=2))) as client:
        login = await _statuses(client, 3, path="/api/auth/login")
        analyze = await _statuses(client, 2)  # separate bucket, unaffected
    assert login == [200, 200, 429]
    assert analyze == [200, 200]


@pytest.mark.asyncio
async def test_provider_probe_endpoints_use_the_admin_bucket(env):
    env(TRUSTED_PROXY_COUNT="0")
    async with _client(_rate_limited_app(_limits(admin_per_minute=2))) as client:
        probe = await _statuses(client, 3, path="/api/ai/grok/health", method="GET")
        other = await _statuses(client, 3, path="/api/ai/providers", method="GET")
    # Before: /api/ai/{id}/health fell through to the default bucket.
    assert probe == [200, 200, 429]
    assert other == [200, 200, 200]


def test_idle_buckets_are_pruned():
    middleware = RateLimitMiddleware(FastAPI(), settings=_limits())
    middleware._bucket_for("ip:198.51.100.1", "analyze", 30)
    middleware._bucket_for("ip:198.51.100.2", "analyze", 30)
    assert len(middleware._buckets) == 2

    middleware._buckets[("ip:198.51.100.1", "analyze")].last_refill -= 10_000  # long idle
    middleware._last_prune -= 10_000  # prune interval elapsed
    middleware._prune(time.monotonic())

    assert list(middleware._buckets) == [("ip:198.51.100.2", "analyze")]


# --------------------------------------------------------------------------
# Unhandled errors are readable by the browser
# --------------------------------------------------------------------------

ALLOWED = "http://localhost:3000"


def _error_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app, allowed_origins=[ALLOWED])
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware, allow_origins=[ALLOWED], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/boom")
    async def boom():
        raise KeyError("simulated bug")

    class DataError(Exception):  # name-matched by the handler, like asyncpg's
        pass

    @app.get("/malformed")
    async def malformed():
        raise DataError("invalid UUID")

    return app


@pytest.mark.asyncio
async def test_unhandled_error_response_carries_cors_headers_and_request_id():
    transport = httpx.ASGITransport(app=_error_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom", headers={"Origin": ALLOWED, "X-Request-Id": "trace-500"})
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert response.json()["errorId"]  # the thing a user is told to quote
    # Before the fix both of these were absent on a 500.
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert response.headers["x-request-id"] == "trace-500"


@pytest.mark.asyncio
async def test_driver_data_error_400_also_carries_cors_headers():
    transport = httpx.ASGITransport(app=_error_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/malformed", headers={"Origin": ALLOWED})
    assert response.status_code == 400
    assert response.headers["access-control-allow-origin"] == ALLOWED


@pytest.mark.asyncio
async def test_error_response_does_not_grant_cors_to_an_unlisted_origin():
    transport = httpx.ASGITransport(app=_error_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        evil = await client.get("/boom", headers={"Origin": "https://evil.example"})
        none = await client.get("/boom")
    assert "access-control-allow-origin" not in evil.headers
    assert "access-control-allow-origin" not in none.headers


# --------------------------------------------------------------------------
# Request id on the access-log line
# --------------------------------------------------------------------------


class _CaptureRequestId(logging.Handler):
    """Records the request-id context variable as seen at emit time — which
    is exactly what the JSON formatter reads when it renders the line."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.seen: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.seen.append((record.getMessage(), request_id_var.get()))


@pytest.mark.asyncio
async def test_access_log_line_carries_the_request_id():
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    logger = logging.getLogger("oxygen.request")
    capture = _CaptureRequestId()
    previous_level = logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ping", headers={"X-Request-Id": "trace-abc-123"})
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)

    assert response.headers["x-request-id"] == "trace-abc-123"
    # Before the fix this line was recorded with request id "-".
    assert ("request", "trace-abc-123") in capture.seen
