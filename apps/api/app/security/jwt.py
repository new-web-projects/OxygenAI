"""
JWT issuance/verification — the stateless half of authentication.

Deliberately paired with a *stateful* check: `security/dependencies.py`
verifies both this token's signature/expiry AND that a matching row
still exists in `sessions` (the existing table, exactly shaped for this:
`token_hash`, `expires_at`, `ip`, `user_agent`). A pure stateless JWT
can't be revoked before it expires; checking a session row too means
"log out" and a future "revoke all sessions" admin action are both real
actions with an immediate effect, not just a client discarding a token
that the server would still accept.

No secret is ever hardcoded. `Settings.auth.jwt_secret` comes from the
environment only, and `AuthSettings.is_configured` is checked before
issuing or verifying anything — an unconfigured deployment fails closed
(auth endpoints report 503, not an insecure default key).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt as pyjwt

from ..config import get_settings


class TokenError(Exception):
    """Raised for any invalid, expired, or malformed token."""


@dataclass(frozen=True)
class TokenClaims:
    user_id: str
    email: str
    role: str
    jti: str
    issued_at: datetime
    expires_at: datetime


def _require_configured() -> str:
    settings = get_settings().auth
    if not settings.is_configured:
        raise TokenError(
            "JWT_SECRET is not configured — authentication is unavailable until it is set"
        )
    return settings.jwt_secret


def create_access_token(user_id: str, email: str, role: str) -> tuple[str, TokenClaims]:
    """Returns (encoded_token, claims). The caller persists a session row keyed by
    `token_hash_for(encoded_token)` — this function only mints the token."""
    secret = _require_configured()
    settings = get_settings().auth
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=settings.access_token_ttl_seconds)
    jti = uuid.uuid4().hex

    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "iss": settings.jwt_issuer,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": jti,
    }
    encoded = pyjwt.encode(payload, secret, algorithm="HS256")
    claims = TokenClaims(
        user_id=user_id, email=email, role=role, jti=jti, issued_at=now, expires_at=expires
    )
    return encoded, claims


def decode_access_token(token: str) -> TokenClaims:
    """Verifies signature, expiry, and issuer. Raises TokenError on any failure —
    callers never need to distinguish exception types, only whether auth succeeded."""
    secret = _require_configured()
    settings = get_settings().auth
    try:
        payload = pyjwt.decode(
            token, secret, algorithms=["HS256"], issuer=settings.jwt_issuer
        )
    except pyjwt.ExpiredSignatureError as err:
        raise TokenError("Token has expired") from err
    except pyjwt.InvalidTokenError as err:
        raise TokenError(f"Invalid token: {err}") from err

    for field in ("sub", "email", "role", "jti", "iat", "exp"):
        if field not in payload:
            raise TokenError(f"Token is missing required claim: {field}")

    return TokenClaims(
        user_id=payload["sub"],
        email=payload["email"],
        role=payload["role"],
        jti=payload["jti"],
        issued_at=datetime.fromtimestamp(payload["iat"], tz=timezone.utc),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    )


def token_hash_for(token: str) -> str:
    """
    SHA-256 of the encoded token — what `sessions.token_hash` stores.
    Never the raw token itself: a leaked database dump must not hand
    over usable bearer tokens.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()