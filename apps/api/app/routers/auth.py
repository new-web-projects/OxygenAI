"""
POST /api/auth/register, POST /api/auth/login, POST /api/auth/logout,
GET /api/auth/me — the endpoints that make `config.AuthSettings` and
`security/jwt.py` real rather than unused scaffolding.

Not named in Passage 1's own endpoint table (which lists Auth only as a
category, "Auth" — the first of Passage 4 §6.3/G13's 19 required
categories), because Document 1 treats an auth *category* as required
without pre-specifying its exact routes — the same disclosed deferral
G13 itself names for several other categories ("Users CRUD... follow the
identical shape and are deliberately deferred to a Phase-1 OpenAPI-spec
task"). This is that task, for Auth specifically, since every other
Phase-1 item in this pass (admin gating) depends on it existing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..config import get_settings
from ..db.users import EmailAlreadyRegisteredError, create_session, create_user, get_user_by_email
from ..errors import ConfigurationError, UnauthorizedError, ValidationError
from ..observability import get_logger
from ..schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from ..security.dependencies import AuthenticatedUser, client_ip, get_current_user
from ..security.jwt import create_access_token, token_hash_for
from ..security.passwords import hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = get_logger("routers.auth")


def _require_auth_configured() -> None:
    if not get_settings().auth.is_configured:
        raise ConfigurationError(
            "Authentication is not configured on this server (JWT_SECRET is unset)."
        )


async def _issue_token(user_id: str, email: str, role: str, request: Request) -> TokenResponse:
    encoded, claims = create_access_token(user_id, email, role)
    await create_session(
        user_id=user_id,
        token_hash=token_hash_for(encoded),
        expires_at=claims.expires_at,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    settings = get_settings().auth
    return TokenResponse(
        accessToken=encoded,
        expiresIn=settings.access_token_ttl_seconds,
        user=UserResponse(id=user_id, email=email, role=role, status="active"),
    )


@router.post("/register")
async def register(body: RegisterRequest, request: Request) -> TokenResponse:
    _require_auth_configured()
    email = body.email.strip().lower()
    try:
        user = await create_user(email, hash_password(body.password))
    except EmailAlreadyRegisteredError as err:
        raise ValidationError("An account with this email already exists") from err
    logger.info("user registered", extra={"userId": user.id})
    return await _issue_token(user.id, user.email, user.role_name, request)


@router.post("/login")
async def login(body: LoginRequest, request: Request) -> TokenResponse:
    _require_auth_configured()
    email = body.email.strip().lower()
    user = await get_user_by_email(email)
    # Constant-shape failure: a nonexistent user and a wrong password
    # both produce the identical 401 — never reveal which one it was.
    if user is None or user.status != "active" or not verify_password(body.password, user.password_hash):
        raise UnauthorizedError("Incorrect email or password")
    logger.info("user logged in", extra={"userId": user.id})
    return await _issue_token(user.id, user.email, user.role_name, request)


@router.post("/logout")
async def logout(request: Request, user: AuthenticatedUser = Depends(get_current_user)) -> dict:
    from ..db.users import delete_session_by_hash

    auth_header = request.headers.get("authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()
    if token:
        await delete_session_by_hash(token_hash_for(token))
    return {"ok": True}


@router.get("/me")
async def me(user: AuthenticatedUser = Depends(get_current_user)) -> UserResponse:
    return UserResponse(id=user.id, email=user.email, role=user.role, status=user.status)