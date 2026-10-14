"""
Authentication policy and dependency behaviour — no database required
(the session/user store is replaced with test doubles), so these run
everywhere and pin the *decisions* the auth layer makes:

* `/api/ai/analyze` requires a JWT by default; `AUTH_REQUIRED=false` is
  the explicit local-development escape hatch (Passage 4 §6.2 / G08b);
* "optional" auth really is optional — an unreachable session store used
  to turn it into a 500 (reproduced), now it degrades to anonymous;
* a required-auth endpoint reports an unreachable store as 503, not a
  misleading 401 and not an unexplained 500;
* the bearer scheme is matched case-insensitively everywhere;
* unknown-email login pays the same bcrypt cost as a wrong password.
"""

from __future__ import annotations

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.config import get_settings
from app.errors import UnauthorizedError
from app.security import dependencies
from app.security.dependencies import (
    AuthBackendUnavailableError,
    bearer_token_from_request,
    get_analysis_user,
    get_current_user,
    get_optional_user,
)
from app.security.jwt import create_access_token, token_hash_for

SECRET = "test-secret-do-not-use-in-production"


@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("JWT_ISSUER", "oxygen-ai-test")
    get_settings(refresh=True)
    yield monkeypatch
    monkeypatch.undo()
    get_settings(refresh=True)


@pytest.fixture
def token(auth_env):
    value, _ = create_access_token("22222222-2222-2222-2222-222222222222", "u@example.com", "user")
    return value


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class _FakeUser:
    id = "22222222-2222-2222-2222-222222222222"
    email = "u@example.com"
    role_name = "user"
    status = "active"


def _healthy_store(monkeypatch):
    async def valid(_hash):
        return True

    async def user(_id):
        return _FakeUser()

    monkeypatch.setattr(dependencies, "session_is_valid", valid)
    monkeypatch.setattr(dependencies, "get_user_by_id", user)


def _broken_store(monkeypatch):
    async def boom(*_a, **_k):
        raise ConnectionError("database is down")

    monkeypatch.setattr(dependencies, "session_is_valid", boom)


# --------------------------------------------------------------------------
# Policy: analyze requires JWT unless explicitly disabled
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analysis_user_requires_credentials_by_default(auth_env):
    with pytest.raises(UnauthorizedError):
        await get_analysis_user(None)


@pytest.mark.asyncio
async def test_analysis_user_allows_anonymous_only_when_explicitly_disabled(auth_env):
    auth_env.setenv("AUTH_REQUIRED", "false")
    get_settings(refresh=True)
    assert await get_analysis_user(None) is None


@pytest.mark.asyncio
async def test_analysis_user_resolves_a_valid_session(auth_env, token):
    _healthy_store(auth_env)
    user = await get_analysis_user(_credentials(token))
    assert user is not None and user.email == "u@example.com" and user.role == "user"


@pytest.mark.asyncio
async def test_revoked_session_is_rejected_even_with_a_valid_signature(auth_env, token):
    async def revoked(_hash):
        return False

    auth_env.setattr(dependencies, "session_is_valid", revoked)
    with pytest.raises(UnauthorizedError):
        await get_current_user(_credentials(token))
    assert await get_optional_user(_credentials(token)) is None


# --------------------------------------------------------------------------
# Store outage behaviour
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_optional_auth_degrades_to_anonymous_when_the_store_is_down(auth_env, token):
    _broken_store(auth_env)
    # Before the fix this raised (-> HTTP 500) despite "optional" auth.
    assert await get_optional_user(_credentials(token)) is None


@pytest.mark.asyncio
async def test_required_auth_reports_a_store_outage_as_503(auth_env, token):
    _broken_store(auth_env)
    with pytest.raises(AuthBackendUnavailableError) as caught:
        await get_current_user(_credentials(token))
    assert caught.value.status_code == 503
    assert caught.value.code == "auth_unavailable"


@pytest.mark.asyncio
async def test_a_garbage_token_never_touches_the_store(auth_env):
    _broken_store(auth_env)  # would raise if consulted
    assert await get_optional_user(_credentials("not.a.jwt")) is None
    with pytest.raises(UnauthorizedError):
        await get_current_user(_credentials("not.a.jwt"))


# --------------------------------------------------------------------------
# Bearer parsing
# --------------------------------------------------------------------------


def _request_with_auth(value: str | None) -> Request:
    headers = [(b"authorization", value.encode())] if value is not None else []
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "bEaReR"])
def test_bearer_scheme_is_case_insensitive(scheme):
    assert bearer_token_from_request(_request_with_auth(f"{scheme} abc.def.ghi")) == "abc.def.ghi"


@pytest.mark.parametrize("value", [None, "", "Bearer", "Basic abc", "abc.def.ghi"])
def test_non_bearer_headers_yield_no_token(value):
    assert bearer_token_from_request(_request_with_auth(value)) == ""


def test_token_hash_matches_what_logout_will_look_up(auth_env, token):
    # login stores token_hash_for(token); logout must derive the same value
    # from whatever scheme casing the client used.
    for scheme in ("Bearer", "bearer"):
        extracted = bearer_token_from_request(_request_with_auth(f"{scheme} {token}"))
        assert token_hash_for(extracted) == token_hash_for(token)


# --------------------------------------------------------------------------
# Login timing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_email_login_still_pays_one_bcrypt_verification(auth_env):
    from app.routers import auth as auth_router
    from app.schemas import LoginRequest

    calls: list[str] = []

    async def no_such_user(_email):
        return None

    def spy_verify(plain, hashed):
        calls.append(hashed)
        return False

    auth_env.setattr(auth_router, "get_user_by_email", no_such_user)
    auth_env.setattr(auth_router, "verify_password", spy_verify)

    request = Request({"type": "http", "method": "POST", "path": "/api/auth/login", "headers": []})
    with pytest.raises(UnauthorizedError):
        await auth_router.login(LoginRequest(email="nobody@example.com", password="whatever-password"), request)

    # Before: verify_password was short-circuited away for unknown emails,
    # so response time alone revealed which addresses were registered.
    assert len(calls) == 1 and calls[0].startswith("$2")  # a real bcrypt hash
