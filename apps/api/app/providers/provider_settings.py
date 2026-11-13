"""
Effective provider settings — environment first, admin override on top.

Passage 4 §5.2's `ai_provider_settings` (API key, timeout, retries, base URL, region) is
administered through the API (`routers/providers.py`) and cached here, in memory, after
being loaded from Postgres (`runtime_config.py`). The providers ask this module for each
value instead of reading `os.environ` directly:

    effective value  =  admin override (if one is stored)  >  environment variable  >  default

so every existing `.env`-only deployment behaves exactly as before, and an override is an
explicit, auditable act. Decrypted keys live only in this process's memory — never in a
log, a response, or an audit row. If the database holds an encrypted key that cannot be
decrypted (the master key is missing or was rotated out), the override is *ignored*, the
provider keeps using its environment variable, and the reason is surfaced to admins
(`key_error`) rather than silently breaking the provider.

`region` is stored and returned but not yet consumed by any provider: it is reserved for the
Vertex AI initialisation Passage 1 §4.3.1 mentions ("only initialization changed"), which is
not built. It is not wired to anything just to make a column look used.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from ..observability import get_logger
from .transport import TransportPolicy

logger = get_logger("providers.settings")


@dataclass(frozen=True)
class ProviderOverrides:
    api_key: str | None = None
    base_url: str | None = None
    timeout_ms: int | None = None
    retry_count: int | None = None
    region: str | None = None
    # Why a stored key is not in effect (e.g. cannot be decrypted); None when fine.
    key_error: str | None = None
    # A key is stored in the database (whether or not it could be decrypted).
    key_stored: bool = False


_overrides: dict[str, ProviderOverrides] = {}
_flags: dict[str, tuple[bool, int]] = {}  # provider -> (enabled, priority)


def set_overrides(mapping: dict[str, ProviderOverrides]) -> None:
    global _overrides
    _overrides = dict(mapping)


def get_overrides(provider_id: str) -> ProviderOverrides:
    return _overrides.get(provider_id, ProviderOverrides())


def set_provider_flags(mapping: dict[str, tuple[bool, int]]) -> None:
    global _flags
    _flags = dict(mapping)


def is_provider_enabled(provider_id: str) -> bool:
    """Providers are enabled unless an admin has disabled them (`ai_providers.enabled`)."""
    return _flags.get(provider_id, (True, 0))[0]


def provider_priority(provider_id: str) -> int:
    return _flags.get(provider_id, (True, 0))[1]


def reset_for_tests() -> None:
    set_overrides({})
    set_provider_flags({})


def effective_api_key(provider_id: str, env_name: str) -> str:
    override = get_overrides(provider_id).api_key
    if override:
        return override
    return os.environ.get(env_name, "").strip()


def api_key_source(provider_id: str, env_name: str) -> str:
    if get_overrides(provider_id).api_key:
        return "admin_override"
    return "environment" if os.environ.get(env_name, "").strip() else "none"


def effective_base_url(provider_id: str, env_name: str | None, default: str) -> str:
    override = get_overrides(provider_id).base_url
    if override:
        return override.rstrip("/")
    if env_name:
        configured = os.environ.get(env_name, "").strip()
        if configured:
            return configured.rstrip("/")
    return default.rstrip("/")


def base_url_source(provider_id: str, env_name: str | None) -> str:
    if get_overrides(provider_id).base_url:
        return "admin_override"
    return "environment" if env_name and os.environ.get(env_name, "").strip() else "default"


def effective_policy(provider_id: str) -> TransportPolicy:
    """The shared transport policy with this provider's admin overrides (if any) applied."""
    policy = TransportPolicy.from_settings()
    ovr = get_overrides(provider_id)
    if ovr.timeout_ms is not None:
        policy = replace(policy, timeout_seconds=ovr.timeout_ms / 1000.0)
    if ovr.retry_count is not None:
        # retry_count is *retries*, so attempts is retries + 1 — same convention as the default.
        policy = replace(policy, max_attempts=max(1, ovr.retry_count + 1))
    return policy
