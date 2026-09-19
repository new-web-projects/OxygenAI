"""
Auth integration tests — the live FastAPI app (via ASGI transport, no
real socket) against a real Postgres. Skipped, not failed, without
DATABASE_URL, matching `test_integration_db.py`'s convention.

Covers the full register -> login -> me -> logout round trip, the two
failure paths that must be indistinguishable (nonexistent email vs.
wrong password), RBAC on the admin-gated provider endpoints, and the
property that a role change takes effect on a user's *existing* token
immediately (because `security/dependencies.py` re-reads the role from
the database on every request rather than trusting the JWT's own role
claim) — a genuine security property worth a regression test, not just
an implementation detail.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL is not set — auth integration tests require a live Postgres instance",
)


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-do-not-use-in-production")
    monkeypatch.setenv("JWT_ISSUER", "oxygen-ai-test")
    monkeypatch.setenv("ACCESS_TOKEN_TTL_SECONDS", "3600")
    from app.config import get_settings

    get_settings(refresh=True)

    import app.db.client as db_client

    db_client._pool = None

    import httpx

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    db_client._pool = None
    get_settings(refresh=True)


@pytest.fixture
async def pool():
    import app.db.client as db_client

    db_client._pool = None
    from app.db.client import get_pool

    p = await get_pool()
    yield p
    db_client._pool = None


def _unique_email() -> str:
    return f"zztest-{uuid.uuid4().hex[:12]}@example.com"


async def _cleanup(pool, email: str) -> None:
    await pool.execute(
        "DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE email = $1)", email
    )
    await pool.execute("DELETE FROM users WHERE email = $1", email)


@pytest.mark.asyncio
async def test_register_then_me_round_trip(client, pool):
    email = _unique_email()
    try:
        resp = await client.post("/api/auth/register", json={"email": email, "password": "correct-horse-battery-staple"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["user"]["email"] == email
        assert body["user"]["role"] == "user"
        assert body["tokenType"] == "bearer"
        token = body["accessToken"]

        me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["email"] == email
    finally:
        await _cleanup(pool, email)


@pytest.mark.asyncio
async def test_duplicate_registration_is_rejected(client, pool):
    email = _unique_email()
    try:
        first = await client.post("/api/auth/register", json={"email": email, "password": "correct-horse-battery-staple"})
        assert first.status_code == 200
        second = await client.post("/api/auth/register", json={"email": email, "password": "a-different-password-123"})
        assert second.status_code == 400
        assert "already exists" in second.json()["error"].lower()
    finally:
        await _cleanup(pool, email)


@pytest.mark.asyncio
async def test_login_with_correct_password_succeeds(client, pool):
    email = _unique_email()
    try:
        await client.post("/api/auth/register", json={"email": email, "password": "correct-horse-battery-staple"})
        resp = await client.post("/api/auth/login", json={"email": email, "password": "correct-horse-battery-staple"})
        assert resp.status_code == 200
        assert resp.json()["user"]["email"] == email
    finally:
        await _cleanup(pool, email)


@pytest.mark.asyncio
async def test_login_failures_are_indistinguishable(client, pool):
    """A nonexistent email and a wrong password must return the identical
    401 shape — distinguishing them would leak which emails are registered."""
    email = _unique_email()
    try:
        await client.post("/api/auth/register", json={"email": email, "password": "correct-horse-battery-staple"})

        wrong_password = await client.post("/api/auth/login", json={"email": email, "password": "totally-wrong"})
        nonexistent = await client.post("/api/auth/login", json={"email": _unique_email(), "password": "totally-wrong"})

        assert wrong_password.status_code == 401
        assert nonexistent.status_code == 401
        assert wrong_password.json()["error"] == nonexistent.json()["error"]
    finally:
        await _cleanup(pool, email)


@pytest.mark.asyncio
async def test_me_without_a_token_is_rejected(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_with_a_malformed_token_is_rejected(client):
    resp = await client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_logout_immediately_revokes_the_session(client, pool):
    """The session-table check, not just the JWT's own expiry — logout
    must take effect on the very next request, not wait for the token
    to naturally expire."""
    email = _unique_email()
    try:
        registered = await client.post("/api/auth/register", json={"email": email, "password": "correct-horse-battery-staple"})
        token = registered.json()["accessToken"]
        headers = {"Authorization": f"Bearer {token}"}

        before = await client.get("/api/auth/me", headers=headers)
        assert before.status_code == 200

        logout = await client.post("/api/auth/logout", headers=headers)
        assert logout.status_code == 200

        after = await client.get("/api/auth/me", headers=headers)
        assert after.status_code == 401
    finally:
        await _cleanup(pool, email)


@pytest.mark.asyncio
async def test_admin_gated_endpoint_rejects_a_plain_user(client, pool):
    email = _unique_email()
    try:
        registered = await client.post("/api/auth/register", json={"email": email, "password": "correct-horse-battery-staple"})
        token = registered.json()["accessToken"]
        resp = await client.post(
            "/api/ai/mock/test", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403
    finally:
        await _cleanup(pool, email)


@pytest.mark.asyncio
async def test_admin_gated_endpoint_accepts_a_promoted_user_without_a_new_login(client, pool):
    """
    Confirms role changes take effect on an *existing* token: the auth
    dependency re-reads the role from the users table on every request
    rather than trusting the JWT's own (now-stale) role claim.
    """
    email = _unique_email()
    try:
        registered = await client.post("/api/auth/register", json={"email": email, "password": "correct-horse-battery-staple"})
        token = registered.json()["accessToken"]

        admin_role_id = await pool.fetchval("SELECT id FROM roles WHERE name = 'admin'")
        assert admin_role_id is not None, "migration 0004_seed_roles.sql must be applied first"
        await pool.execute("UPDATE users SET role_id = $1 WHERE email = $2", admin_role_id, email)

        resp = await client.post(
            "/api/ai/mock/test", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
    finally:
        await _cleanup(pool, email)


@pytest.mark.asyncio
async def test_route_endpoint_requires_authentication_but_not_admin(client, pool):
    email = _unique_email()
    try:
        registered = await client.post("/api/auth/register", json={"email": email, "password": "correct-horse-battery-staple"})
        token = registered.json()["accessToken"]

        anonymous = await client.post("/api/ai/route", json={"provider": "mock"})
        assert anonymous.status_code == 401

        authenticated = await client.post(
            "/api/ai/route", json={"provider": "mock"}, headers={"Authorization": f"Bearer {token}"}
        )
        assert authenticated.status_code == 200
    finally:
        await _cleanup(pool, email)


@pytest.mark.asyncio
async def test_analyze_endpoint_still_works_with_no_token_at_all(client):
    """The compatibility guarantee this whole design exists to preserve:
    the live frontend's one real endpoint must keep working anonymously."""
    resp = await client.post("/api/ai/analyze", json={"symbol": "ZZTESTAUTH", "provider": "mock"})
    assert resp.status_code == 200
    assert resp.json()["status"] in ("SETUP_FOUND", "NO_VALID_SETUP")


@pytest.mark.asyncio
async def test_password_shorter_than_twelve_characters_is_rejected(client):
    resp = await client.post(
        "/api/auth/register", json={"email": _unique_email(), "password": "short"}
    )
    assert resp.status_code == 400  # Pydantic min_length=12 on RegisterRequest