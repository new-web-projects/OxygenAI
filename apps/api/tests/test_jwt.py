from __future__ import annotations

import time

import pytest

from app.security.jwt import TokenError, create_access_token, decode_access_token, token_hash_for


@pytest.fixture(autouse=True)
def _configured_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-do-not-use-in-production")
    monkeypatch.setenv("JWT_ISSUER", "oxygen-ai-test")
    monkeypatch.setenv("ACCESS_TOKEN_TTL_SECONDS", "3600")
    from app.config import get_settings

    get_settings(refresh=True)
    yield
    get_settings(refresh=True)


def test_round_trip_preserves_claims():
    token, claims = create_access_token("user-1", "a@b.com", "user")
    decoded = decode_access_token(token)
    assert decoded.user_id == "user-1"
    assert decoded.email == "a@b.com"
    assert decoded.role == "user"
    assert decoded.jti == claims.jti


def test_tampered_token_is_rejected():
    token, _ = create_access_token("user-1", "a@b.com", "user")
    tampered = token[:-4] + ("A" * 4 if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(TokenError):
        decode_access_token(tampered)


def test_wrong_secret_is_rejected(monkeypatch):
    token, _ = create_access_token("user-1", "a@b.com", "user")
    monkeypatch.setenv("JWT_SECRET", "a-completely-different-secret")
    from app.config import get_settings

    get_settings(refresh=True)
    with pytest.raises(TokenError):
        decode_access_token(token)


def test_expired_token_is_rejected(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_TTL_SECONDS", "0")
    from app.config import get_settings

    get_settings(refresh=True)
    token, _ = create_access_token("user-1", "a@b.com", "user")
    time.sleep(1.1)
    with pytest.raises(TokenError):
        decode_access_token(token)


def test_wrong_issuer_is_rejected(monkeypatch):
    token, _ = create_access_token("user-1", "a@b.com", "user")
    monkeypatch.setenv("JWT_ISSUER", "someone-elses-issuer")
    from app.config import get_settings

    get_settings(refresh=True)
    with pytest.raises(TokenError):
        decode_access_token(token)


def test_unconfigured_secret_fails_closed_not_with_a_default_key(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    from app.config import get_settings

    get_settings(refresh=True)
    with pytest.raises(TokenError):
        create_access_token("user-1", "a@b.com", "user")


def test_token_hash_is_deterministic_and_not_reversible_to_the_token():
    token, _ = create_access_token("user-1", "a@b.com", "user")
    h1 = token_hash_for(token)
    h2 = token_hash_for(token)
    assert h1 == h2
    assert h1 != token
    assert len(h1) == 64  # sha256 hex digest length


def test_two_tokens_for_the_same_user_have_different_hashes():
    """Each login mints a distinct session — hashes must not collide across calls."""
    token_a, _ = create_access_token("user-1", "a@b.com", "user")
    token_b, _ = create_access_token("user-1", "a@b.com", "user")
    assert token_hash_for(token_a) != token_hash_for(token_b)