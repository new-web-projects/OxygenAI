from __future__ import annotations

import pytest

from app.security.passwords import hash_password, verify_password


def test_hash_is_not_the_plaintext():
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert hashed.startswith("$2b$")  # bcrypt's own format marker


def test_verify_accepts_the_correct_password():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True


def test_verify_rejects_the_wrong_password():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("wrong password", hashed) is False


def test_same_password_hashes_differently_each_time():
    """bcrypt salts per-call — two hashes of the same password must differ,
    which is what makes a rainbow-table attack against the DB pointless."""
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    assert verify_password("same-password", a)
    assert verify_password("same-password", b)


def test_empty_password_is_rejected_at_hash_time():
    with pytest.raises(ValueError):
        hash_password("")


def test_verify_returns_false_not_an_exception_on_a_malformed_hash():
    """A corrupt stored hash must fail closed, never raise past the auth boundary."""
    assert verify_password("anything", "not-a-real-bcrypt-hash") is False


def test_verify_returns_false_on_empty_inputs():
    assert verify_password("", "somehash") is False
    assert verify_password("something", "") is False