"""
Shared pytest fixtures.

The circuit breaker registry (`app.providers.circuit_breaker`) is a
process-level singleton by design — Passage 1 §4.5's router state is
meant to persist across requests within one running API instance. Left
alone across a pytest session, though, that means failures recorded by
one test file accumulate toward another file's threshold, making the
suite's pass/fail depend on file execution order. This fixture resets it
before every test, so each test starts from a clean CLOSED breaker
regardless of what ran before it.

The database connection pool (`app.db.client._pool`) has the same
process-level-singleton shape and the same test-suite problem, but for a
different underlying reason: an asyncpg pool is bound to the event loop
it was created in, and pytest.ini's `asyncio_default_fixture_loop_scope
= function` gives every test its own event loop. A pool created by test
A is unusable by the time test B runs against a fresh loop, and raises
"Event loop is closed" from deep inside asyncpg — not from anything
wrong in the test itself. Several test files originally worked around
this with a local `pool` fixture that reset `_pool` to None; moved here
as an autouse fixture so any test that touches the database gets a
correctly-scoped pool automatically, including ones (like a handful of
tool-registry tests) that call database-backed code indirectly without
necessarily expecting to need this.
"""

from __future__ import annotations

import os

import pytest

# The in-process ASGI test transport has no real peer address, so every
# request in the suite would share a single "ip:unknown" rate-limit bucket
# and the (deliberately tight) credential-endpoint limit would trip across
# unrelated tests. Rate limiting is therefore off for the suite by default
# and is covered directly by tests/test_gateway_security.py, which builds
# its own app around `RateLimitMiddleware` with explicit settings. Must be
# set before `app.main` is first imported (it captures settings at import).
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

from app.providers.circuit_breaker import reset_circuit_breakers_for_tests


@pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    reset_circuit_breakers_for_tests()
    yield
    reset_circuit_breakers_for_tests()


@pytest.fixture(autouse=True)
async def _reset_db_pool():
    import app.db.client as db_client

    db_client._pool = None
    yield
    db_client._pool = None

@pytest.fixture(autouse=True)
def _reset_runtime_config_singletons():
    """
    P3 made the model registry, per-model health memory and provider overrides process-level state
    that a database load or an admin write can change. Reset them around every test so no test
    inherits another's loaded registry, disabled provider or stored API key.
    """
    from app.providers import provider_settings
    from app.providers.model_health import reset_model_health_for_tests
    from app.providers.model_registry import reset_model_registry_for_tests

    reset_model_registry_for_tests()
    reset_model_health_for_tests()
    provider_settings.reset_for_tests()
    yield
    reset_model_registry_for_tests()
    reset_model_health_for_tests()
    provider_settings.reset_for_tests()


# ---------------------------------------------------------------------------
# Shared fixtures for the database-backed P3 tests (test_p3_persistence.py, test_p3_api.py)
# ---------------------------------------------------------------------------

P3_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
async def p3_pool():
    from app.db.client import get_pool

    pool = await get_pool()
    yield pool


@pytest.fixture
async def p3_client(monkeypatch):
    """An ASGI client against the real app with JWT configured. (No lifespan: tests load config explicitly.)"""
    import httpx

    monkeypatch.setenv("JWT_SECRET", "test-secret-do-not-use-in-production")
    monkeypatch.setenv("JWT_ISSUER", "oxygen-ai-test")
    monkeypatch.setenv("ACCESS_TOKEN_TTL_SECONDS", "3600")
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "0")
    from app.config import get_settings

    get_settings(refresh=True)
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    get_settings(refresh=True)


@pytest.fixture
async def p3_accounts(p3_pool):
    import uuid

    created: list[str] = []

    async def make(client, *, role: str = "user"):
        email = f"zzp3-{uuid.uuid4().hex[:12]}@example.com"
        created.append(email)
        registered = await client.post("/api/auth/register", json={"email": email, "password": P3_PASSWORD})
        assert registered.status_code == 200, registered.text
        if role != "user":
            from app.db.users import set_user_role

            assert await set_user_role(email, role)
        user_id = str(await p3_pool.fetchval("SELECT id FROM users WHERE email = $1", email))
        return user_id, {"Authorization": f"Bearer {registered.json()['accessToken']}"}

    yield make

    for email in created:
        sub = "(SELECT id FROM users WHERE email = $1)"
        await p3_pool.execute(f"DELETE FROM audit_logs WHERE actor_id IN {sub}", email)
        await p3_pool.execute(f"DELETE FROM conversations WHERE user_id IN {sub}", email)
        await p3_pool.execute(f"DELETE FROM usage_logs WHERE user_id IN {sub}", email)
        await p3_pool.execute(f"DELETE FROM error_logs WHERE user_id IN {sub}", email)
        await p3_pool.execute("DELETE FROM users WHERE email = $1", email)


@pytest.fixture
async def p3_admin_state(p3_pool):
    """
    Seeds the model catalogue into the database (as the API does at startup), then restores every
    admin-managed table — ai_models, ai_provider_settings, ai_providers flags, system_settings
    (breaker config / seeded-catalogue marker) — to exactly what it was, however the test changed
    them. Rows a test created are removed; rows it modified are put back.
    """
    import json

    from app.providers.runtime_config import sync_catalogue

    await sync_catalogue()
    models = await p3_pool.fetch(
        "SELECT id, provider_id, model_id, display_name, status, config, is_default, is_fallback FROM ai_models"
    )
    settings_rows = await p3_pool.fetch("SELECT * FROM ai_provider_settings")
    flags = await p3_pool.fetch("SELECT id, enabled, priority FROM ai_providers")
    system = await p3_pool.fetch(
        "SELECT key, value FROM system_settings WHERE key IN ('circuit_breaker', 'model_catalogue_seeded')"
    )
    seeded_before = {r["key"]: r["value"] for r in system}

    yield

    from app.db.client import get_pool

    p3_pool = await get_pool()  # a lifespan-shutdown test closes the pool the fixture started with
    known = {r["id"] for r in models}
    await p3_pool.execute("DELETE FROM ai_models WHERE NOT (id = ANY($1::uuid[]))", list(known))
    for r in models:  # a test may have deleted a snapshot row; put it back
        await p3_pool.execute(
            "INSERT INTO ai_models (id, provider_id, model_id, display_name, status, config) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb) ON CONFLICT (id) DO NOTHING",
            r["id"], r["provider_id"], r["model_id"], r["display_name"], r["status"],
            r["config"] if isinstance(r["config"], str) else json.dumps(r["config"]),
        )
    await p3_pool.execute("UPDATE ai_models SET is_default = false, is_fallback = false")
    for r in models:
        await p3_pool.execute(
            "UPDATE ai_models SET display_name=$2, status=$3, config=$4::jsonb WHERE id=$1",
            r["id"], r["display_name"], r["status"], r["config"] if isinstance(r["config"], str) else json.dumps(r["config"]),
        )
    for r in models:
        await p3_pool.execute(
            "UPDATE ai_models SET is_default=$2, is_fallback=$3 WHERE id=$1", r["id"], r["is_default"], r["is_fallback"]
        )
    await p3_pool.execute("DELETE FROM ai_provider_settings")
    for r in settings_rows:
        await p3_pool.execute(
            "INSERT INTO ai_provider_settings (id, provider_id, api_key_encrypted, timeout_ms, retry_count, base_url, region, updated_by, updated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            r["id"], r["provider_id"], r["api_key_encrypted"], r["timeout_ms"], r["retry_count"], r["base_url"],
            r["region"], r["updated_by"], r["updated_at"],
        )
    for r in flags:
        await p3_pool.execute("UPDATE ai_providers SET enabled=$2, priority=$3 WHERE id=$1", r["id"], r["enabled"], r["priority"])
    await p3_pool.execute("DELETE FROM system_settings WHERE key IN ('circuit_breaker', 'model_catalogue_seeded')")
    for key, value in seeded_before.items():
        await p3_pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, $2::jsonb)", key, value)
    await p3_pool.execute("DELETE FROM error_logs WHERE type = 'ModelRetired'")
    await p3_pool.execute("DELETE FROM audit_logs WHERE action IN ('model.auto_retire')")
