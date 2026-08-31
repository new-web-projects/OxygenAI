"""
Port of lib/providers/index.ts. Exactly the three providers the
blueprint specifies, plus the offline mock fallback. Gemini is never a
provider in its own right, only Gemma's hosted transport
(gemma_provider.py).
"""

from __future__ import annotations

from .base import AIProvider
from .custom_ai_provider import custom_ai_provider
from .gemma_provider import gemma_provider
from .grok_provider import grok_provider
from .mock_provider import mock_provider

_REGISTRY: dict[str, AIProvider] = {
    "mock": mock_provider,
    "custom": custom_ai_provider,
    "grok": grok_provider,
    "gemma": gemma_provider,
}


def resolve_provider(requested_id: str | None) -> AIProvider:
    """
    Same fallback principle as the blueprint's Provider Router: degrade
    to a controlled, working path instead of a hard failure when a
    provider isn't actually configured.
    """
    provider = _REGISTRY.get(requested_id) if requested_id else None
    if provider is None:
        return mock_provider
    if not provider.is_configured():
        return mock_provider
    return provider


def get_provider_strict(provider_id: str) -> AIProvider:
    """
    For multi-provider comparison, unlike resolve_provider(): does NOT
    silently substitute mock for an unconfigured provider. A comparison
    is supposed to show distinct real providers side by side — quietly
    running mock logic under a "Grok" label would defeat the point.
    Unconfigured providers are still returned here (reason() will raise;
    the caller isolates that per slot — see app/comparison.py).
    """
    provider = _REGISTRY.get(provider_id)
    if provider is None:
        raise KeyError(f"Unknown provider id: {provider_id}")
    return provider


def list_providers() -> list[dict]:
    return [
        {"id": p.id, "displayName": p.display_name, "configured": p.is_configured()}
        for p in _REGISTRY.values()
    ]
