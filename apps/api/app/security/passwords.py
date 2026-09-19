"""
Password hashing — bcrypt, a mature, purpose-built, widely-audited
choice for this exact job (not a general hash function misused for
passwords). Cost factor 12 is bcrypt's own current recommended default:
expensive enough to resist offline brute-forcing at present hardware
speeds, cheap enough not to noticeably slow a login request.

No plaintext password is ever logged, stored, or returned in any
response — `users.password_hash` (the existing column, unchanged) is the
only place a password-derived value is persisted, and it is a one-way
hash, never the password itself.
"""

from __future__ import annotations

import bcrypt

_ROUNDS = 12


def hash_password(plain: str) -> str:
    if not plain:
        raise ValueError("Password must not be empty")
    hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=_ROUNDS))
    return hashed.decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Returns False on any malformed hash rather than raising — a corrupt
    stored hash must never be treated as a successful login."""
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False