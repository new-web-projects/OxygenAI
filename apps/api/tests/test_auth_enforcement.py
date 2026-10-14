"""
Authentication enforcement end to end — the live FastAPI app (ASGI
transport, no socket) against a real Postgres. Skipped without
DATABASE_URL, like the other integration modules.

Covers the Phase 1 behaviours that need real sessions/users/rows:

* a hostile or proxy-shaped X-Forwarded-For can no longer break login or
  registration (it used to reach `sessions.ip inet` unvalidated);
* logout revokes the session whatever case the client wrote the scheme in
  (it used to return 200 and leave the token working);
* knowledge upload/delete are admin-only; search/list need a login;
* a multi-provider comparison can be rated/preferred only by its owner or
  an admin, and a non-owner cannot tell it exists;
* the `app.cli` bootstrap is the supported way to obtain an admin.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL is not set — auth enforcement tests require a live Postgres instance",
)

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-do-not-use-in-production")
    monkeypatch.setenv("JWT_ISSUER", "oxygen-ai-test")
    monkeypatch.setenv("ACCESS_TOKEN_TTL_SECONDS", "3600")
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "0")
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


@pytest.fixture
async def accounts(pool):
    """Registers throwaway accounts and removes everything they created."""
    created: list[str] = []
    documents: list[str] = []

    async def make(client, *, role: str = "user") -> tuple[str, str, dict[str, str]]:
        email = f"zztest-{uuid.uuid4().hex[:12]}@example.com"
        created.append(email)
        registered = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
        assert registered.status_code == 200, registered.text
        if role != "user":
            from app.db.users import set_user_role

            assert await set_user_role(email, role)
        token = registered.json()["accessToken"]
        user_id = str(await pool.fetchval("SELECT id FROM users WHERE email = $1", email))
        return email, user_id, {"Authorization": f"Bearer {token}"}

    make.documents = documents  # type: ignore[attr-defined]
    yield make

    for document_id in documents:
        await pool.execute("DELETE FROM knowledge_documents WHERE id = $1", document_id)
    for email in created:
        await pool.execute(
            "DELETE FROM conversations WHERE user_id IN (SELECT id FROM users WHERE email = $1)", email
        )
        await pool.execute(
            "DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE email = $1)", email
        )
        await pool.execute("DELETE FROM users WHERE email = $1", email)


# --------------------------------------------------------------------------
# Network identity reaching the sessions table
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forwarded, expected_ip",
    [
        ("unknown", "127.0.0.1"),  # unparseable -> falls back to the socket peer
        ("203.0.113.9:51234", "203.0.113.9"),  # PaaS-style ip:port -> reduced to the address
        ("6.6.6.6, 203.0.113.9", "203.0.113.9"),  # client-prepended junk is ignored
    ],
)
async def test_hostile_forwarded_for_cannot_break_registration_or_login(
    client, pool, monkeypatch, forwarded, expected_ip
):
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "1")
    from app.config import get_settings

    get_settings(refresh=True)

    email = f"zztest-{uuid.uuid4().hex[:12]}@example.com"
    try:
        registered = await client.post(
            "/api/auth/register",
            json={"email": email, "password": PASSWORD},
            headers={"X-Forwarded-For": forwarded},
        )
        # Before the fix: 400 "identifiers malformed", with the account already created.
        assert registered.status_code == 200, registered.text
        logged_in = await client.post(
            "/api/auth/login",
            json={"email": email, "password": PASSWORD},
            headers={"X-Forwarded-For": forwarded},
        )
        assert logged_in.status_code == 200, logged_in.text

        stored = await pool.fetch(
            "SELECT ip FROM sessions WHERE user_id = (SELECT id FROM users WHERE email = $1)", email
        )
        assert {str(row["ip"]) for row in stored} == {expected_ip}
    finally:
        await pool.execute(
            "DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE email = $1)", email
        )
        await pool.execute("DELETE FROM users WHERE email = $1", email)


# --------------------------------------------------------------------------
# Logout
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
async def test_logout_revokes_the_token_whatever_case_the_scheme_is_written_in(client, accounts, scheme):
    _, _, headers = await accounts(client)
    token = headers["Authorization"].split(" ", 1)[1]

    assert (await client.get("/api/auth/me", headers=headers)).status_code == 200
    logout = await client.post("/api/auth/logout", headers={"Authorization": f"{scheme} {token}"})
    assert logout.status_code == 200
    # Before the fix a lowercase scheme returned 200 here and this stayed 200.
    assert (await client.get("/api/auth/me", headers=headers)).status_code == 401


# --------------------------------------------------------------------------
# Knowledge authorization
# --------------------------------------------------------------------------

UPLOAD = {"title": "zztest policy note", "content": "Position sizing must cap risk per trade.", "sourceType": "knowledge"}


@pytest.mark.asyncio
async def test_knowledge_endpoints_reject_anonymous_callers(client):
    assert (await client.post("/api/knowledge/search", json={"query": "risk"})).status_code == 401
    assert (await client.get("/api/knowledge/documents")).status_code == 401
    assert (await client.post("/api/knowledge/upload", json=UPLOAD)).status_code == 401
    assert (await client.delete(f"/api/knowledge/documents/{uuid.uuid4()}")).status_code == 401


@pytest.mark.asyncio
async def test_a_plain_user_can_search_and_list_but_not_upload_or_delete(client, accounts):
    _, _, user = await accounts(client)
    assert (await client.post("/api/knowledge/search", json={"query": "risk"}, headers=user)).status_code == 200
    assert (await client.get("/api/knowledge/documents", headers=user)).status_code == 200
    assert (await client.post("/api/knowledge/upload", json=UPLOAD, headers=user)).status_code == 403
    assert (await client.delete(f"/api/knowledge/documents/{uuid.uuid4()}", headers=user)).status_code == 403


@pytest.mark.asyncio
async def test_an_admin_can_upload_and_delete_a_document(client, accounts):
    _, _, admin = await accounts(client, role="admin")
    uploaded = await client.post("/api/knowledge/upload", json=UPLOAD, headers=admin)
    assert uploaded.status_code == 200, uploaded.text
    document_id = uploaded.json()["documentId"]
    accounts.documents.append(document_id)

    deleted = await client.delete(f"/api/knowledge/documents/{document_id}", headers=admin)
    assert deleted.status_code == 200


# --------------------------------------------------------------------------
# Comparison ownership
# --------------------------------------------------------------------------


async def _run_comparison(client, headers) -> str:
    response = await client.post(
        "/api/ai/analyze", json={"symbol": "ZZOWN", "providers": ["mock", "grok"]}, headers=headers
    )
    assert response.status_code == 200, response.text
    comparison_id = response.json()["comparisonId"]
    assert comparison_id, "the comparison should have been persisted"
    return comparison_id


@pytest.mark.asyncio
async def test_only_the_owner_or_an_admin_can_rate_a_comparison(client, pool, accounts):
    _, owner_id, owner = await accounts(client)
    _, _, stranger = await accounts(client)
    _, _, admin = await accounts(client, role="admin")

    comparison_id = await _run_comparison(client, owner)
    stored_owner = await pool.fetchval(
        "SELECT c.user_id FROM ai_comparisons ac JOIN conversations c ON c.id = ac.conversation_id WHERE ac.id = $1",
        comparison_id,
    )
    assert str(stored_owner) == owner_id

    url = f"/api/ai/comparisons/{comparison_id}"
    # A non-owner gets the same 404 as a nonexistent id — no existence probing.
    assert (await client.patch(url, json={"rating": 1}, headers=stranger)).status_code == 404
    assert (await client.patch(f"/api/ai/comparisons/{uuid.uuid4()}", json={"rating": 1}, headers=stranger)).status_code == 404
    assert (await client.patch(url, json={"rating": 1})).status_code == 401  # anonymous

    mine = await client.patch(url, json={"rating": 4}, headers=owner)
    assert mine.status_code == 200 and mine.json()["userRating"] == 4
    theirs = await client.patch(url, json={"rating": 5}, headers=admin)
    assert theirs.status_code == 200 and theirs.json()["userRating"] == 5


# --------------------------------------------------------------------------
# Admin bootstrap CLI
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_create_admin_yields_an_account_that_can_log_in_as_admin(client, pool):
    from app import cli

    email = f"zztest-{uuid.uuid4().hex[:12]}@example.com"
    try:
        message = await cli._create_admin(email, PASSWORD)
        assert "created" in message

        login = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}
        assert (await client.get("/api/auth/me", headers=headers)).json()["role"] == "admin"

        # Re-running promotes, never overwrites the password.
        again = await cli._create_admin(email, "a-different-password-entirely")
        assert "already existed" in again
        assert (await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})).status_code == 200

        demoted = await cli._set_role(email, "user")
        assert "now 'user'" in demoted
        assert (await client.get("/api/auth/me", headers=headers)).json()["role"] == "user"
        assert "nothing changed" in await cli._set_role("nobody-here@example.com", "admin")
    finally:
        from app.db.client import get_pool

        p = await get_pool()
        await p.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE email = $1)", email)
        await p.execute("DELETE FROM users WHERE email = $1", email)
