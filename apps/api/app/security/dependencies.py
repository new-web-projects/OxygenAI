"""
FastAPI dependencies for authentication and RBAC.

Three current-user dependencies, deliberately different failure modes:

* `get_current_user` — REQUIRED. No/invalid/expired/revoked token → 401;
  auth backend (database) unreachable → 503. Used on admin endpoints and
  every user-scoped endpoint.
* `get_optional_user` — OPTIONAL. No/invalid token → None and the request
  proceeds anonymously. If the auth backend is unreachable it ALSO
  degrades to anonymous (previously it surfaced as a 500: a validly
  signed token plus a database outage broke an endpoint documented as
  "optional auth").
* `get_analysis_user` — the policy dependency for the analysis surface
  (`POST /api/ai/analyze`, comparison feedback). Passage 4 §6.2 (G08b)
  states analyze is "Auth: required (JWT)", so this is `get_current_user`
  unless `AUTH_REQUIRED=false` is set explicitly for local development,
  in which case it is `get_optional_user`. The interim "accept but do not
  require a token" behaviour of the previous pass is therefore retired:
  the frontend now ships a login flow (see `app/components/AuthPanel.tsx`).

All verify the JWT (signature, expiry, issuer) AND that a matching
`sessions` row still exists — logout revokes a token immediately, not at
its natural expiry.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import get_settings
from ..db.users import UserRecord, get_permissions_for_role, get_user_by_id, session_is_valid
from ..errors import ApiError, ForbiddenError, UnauthorizedError
from ..observability import get_logger
from .jwt import TokenError, decode_access_token, token_hash_for
from .network import client_ip  # noqa: F401 — re-exported; auth.py and older imports use this path

logger = get_logger("security")

# FastAPI's HTTPBearer matches the scheme case-insensitively ("Bearer",
# "bearer", "BEARER"), so every token-reading site must too — see
# `bearer_token_from_request`.
_bearer_optional = HTTPBearer(auto_error=False)


class AuthBackendUnavailableError(ApiError):
    """The session/user store could not be reached — not the caller's fault."""

    status_code = 503
    code = "auth_unavailable"


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str
    role: str
    status: str


def bearer_token_from_request(request: Request) -> str:
    """
    Extract the bearer token the same way `HTTPBearer` does: scheme
    compared case-insensitively. `logout` previously used
    `removeprefix("Bearer ")`, so `Authorization: bearer <token>` was
    accepted for authentication but silently failed to revoke the
    session — logout returned 200 and the token kept working.
    """
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


async def _resolve(token: str) -> AuthenticatedUser | None:
    """
    Returns the user, or None for any rejection (bad/expired/revoked
    token, inactive user). Raises AuthBackendUnavailableError only when
    the session/user store itself fails.
    """
    try:
        claims = decode_access_token(token)
    except TokenError as err:
        logger.info("token rejected", extra={"reason": str(err)})
        return None

    try:
        if not await session_is_valid(token_hash_for(token)):
            logger.info("token valid but session was revoked or expired", extra={"jti": claims.jti})
            return None
        user = await get_user_by_id(claims.user_id)
    except Exception as err:  # noqa: BLE001 — any store failure is "backend unavailable"
        logger.warning("auth backend unavailable", extra={"error": str(err)})
        raise AuthBackendUnavailableError(
            "Authentication service is temporarily unavailable"
        ) from err

    if user is None or user.status != "active":
        return None

    return AuthenticatedUser(id=user.id, email=user.email, role=user.role_name, status=user.status)


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
) -> AuthenticatedUser | None:
    if credentials is None or not credentials.credentials:
        return None
    try:
        return await _resolve(credentials.credentials)
    except AuthBackendUnavailableError:
        # Optional means optional: an unreachable store must not turn an
        # anonymous-capable endpoint into an error.
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
) -> AuthenticatedUser:
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Authentication required — send an Authorization: Bearer <token> header")
    user = await _resolve(credentials.credentials)
    if user is None:
        raise UnauthorizedError("Invalid, expired, or revoked token")
    return user


async def get_analysis_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
) -> AuthenticatedUser | None:
    """Policy dependency: JWT required unless AUTH_REQUIRED=false (dev only)."""
    if get_settings().auth.auth_required:
        return await get_current_user(credentials)
    return await get_optional_user(credentials)


def require_role(*allowed_roles: str):
    """
    Dependency factory: `Depends(require_role("admin"))`. Layers on top
    of `get_current_user`, so an unauthenticated request still gets 401
    (not 403) — 403 means "we know who you are, and it isn't enough."
    """

    async def _check(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if user.role not in allowed_roles:
            raise ForbiddenError(
                f"Role '{user.role}' is not permitted here — requires one of: {', '.join(allowed_roles)}"
            )
        return user

    return _check


def require_permission(resource: str, action: str):
    """
    Finer-grained than `require_role`: checks the `permissions` table
    directly rather than hardcoding a role name at the call site, so
    permission changes (migration 0004's seed rows, or a future
    admin-editable grant) take effect without a code change.
    """

    async def _check(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        granted = await get_permissions_for_role(user.role)
        if (resource, action) not in granted:
            raise ForbiddenError(
                f"Role '{user.role}' lacks permission '{action}' on '{resource}'"
            )
        return user

    return _check
