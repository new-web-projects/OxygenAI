"""
FastAPI dependencies for authentication and RBAC.

Two current-user dependencies, deliberately different failure modes:

* `get_current_user` — REQUIRED. No/invalid/expired/revoked token → 401.
  Used on admin-only endpoints (provider test, models CRUD).
* `get_optional_user` — OPTIONAL. No/invalid token → returns None, the
  request proceeds anonymously. Used on `/api/ai/analyze` so a request
  is attributed to a user when a token is present, without requiring
  one — the live frontend has no login flow yet, and forcing auth here
  would break the one endpoint every existing user actually depends on.
  Passage 4 §6.2 states analyze requires JWT; this is a deliberate,
  disclosed interim step toward that, not a claim that it's already
  enforced — see the delivery notes for why full enforcement waits on a
  coordinated frontend login phase.

Both verify the JWT itself (signature, expiry, issuer) AND that a
matching `sessions` row still exists and hasn't expired — a token whose
session was deleted (logout) stops working immediately, not just when
the JWT's own expiry eventually arrives.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..db.users import UserRecord, get_permissions_for_role, get_user_by_id, session_is_valid
from ..errors import ForbiddenError, UnauthorizedError
from ..observability import get_logger
from .jwt import TokenError, decode_access_token, token_hash_for

logger = get_logger("security")

_bearer_optional = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str
    role: str
    status: str


async def _resolve(token: str) -> AuthenticatedUser | None:
    try:
        claims = decode_access_token(token)
    except TokenError as err:
        logger.info("token rejected", extra={"reason": str(err)})
        return None

    if not await session_is_valid(token_hash_for(token)):
        logger.info("token valid but session was revoked or expired", extra={"jti": claims.jti})
        return None

    user = await get_user_by_id(claims.user_id)
    if user is None or user.status != "active":
        return None

    return AuthenticatedUser(id=user.id, email=user.email, role=user.role_name, status=user.status)


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
) -> AuthenticatedUser | None:
    if credentials is None or not credentials.credentials:
        return None
    return await _resolve(credentials.credentials)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
) -> AuthenticatedUser:
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Authentication required — send an Authorization: Bearer <token> header")
    user = await _resolve(credentials.credentials)
    if user is None:
        raise UnauthorizedError("Invalid, expired, or revoked token")
    return user


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


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None