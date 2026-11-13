"""
Encryption for secrets kept in Postgres — today `ai_provider_settings.api_key_encrypted`
(Passage 4 §5.2). Fernet (AES-128-CBC + HMAC-SHA256, from `cryptography`).

The master key comes from the environment and is never invented, generated at
runtime, or stored by this application:

    SETTINGS_ENCRYPTION_KEY=<key>[,<older key>,...]

Generate one yourself, once, and keep it out of source control:

    python -m app.cli generate-settings-key

The first key encrypts; every listed key can decrypt, so rotation is: prepend the
new key, deploy, re-save the secrets (or let admins re-enter them), then drop the
old key. Without a key the store is simply unavailable — saving an API key is
refused with an explicit error rather than falling back to plaintext — and the
providers keep using their environment variables.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

ENV_NAME = "SETTINGS_ENCRYPTION_KEY"


class SecretStoreError(RuntimeError):
    """The secret store cannot encrypt or decrypt (missing/invalid key, or a token it cannot open)."""


def _keys() -> list[str]:
    return [k.strip() for k in os.environ.get(ENV_NAME, "").split(",") if k.strip()]


def is_configured() -> bool:
    """True when the environment holds at least one well-formed key."""
    try:
        _cipher()
    except SecretStoreError:
        return False
    return True


def _cipher() -> MultiFernet:
    keys = _keys()
    if not keys:
        raise SecretStoreError(
            f"{ENV_NAME} is not set. Generate a key with `python -m app.cli generate-settings-key` "
            "and put it in the API's environment before storing provider API keys."
        )
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError) as err:
        raise SecretStoreError(
            f"{ENV_NAME} is not a valid comma-separated list of Fernet keys "
            "(each must be 32 url-safe base64-encoded bytes)."
        ) from err


def generate_key() -> str:
    return Fernet.generate_key().decode()


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        raise SecretStoreError("Refusing to encrypt an empty secret.")
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _cipher().decrypt(token.encode()).decode()
    except InvalidToken as err:
        raise SecretStoreError(
            f"A stored secret could not be decrypted with the keys in {ENV_NAME} "
            "(wrong or rotated-out key, or the value was altered)."
        ) from err
