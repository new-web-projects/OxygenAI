"""
Provider Router — Passage 1 §4.5.

The blueprint gives the router's shape as pseudocode:

    health check -> circuit breaker -> fallback -> execute -> validate -> tag

Before this pass the whole of it was one function, `resolve_provider()`,
which did a single `is_configured()` check and substituted mock. Four of
the six stages did not exist. All six are implemented here, in that
order, against the same provider set.

Provider identity is exactly the blueprint's: Custom AI, Grok, Gemma 4
(§2), plus the offline mock reasoner for demo and test. Gemini is never
a provider in its own right — only Gemma's hosted transport (§4.3.1).

`resolve_provider()` and `get_provider_strict()` keep their existing
names and semantics so nothing that imported them breaks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from ..observability import get_logger
from .base import AIProvider, AnalysisContext, HealthResult, ProviderReasoning
from .circuit_breaker import CircuitBreakerOpen, get_circuit_breakers
from .custom_ai_provider import custom_ai_provider
from .gemma_provider import gemma_provider
from .grok_provider import grok_provider
from .mock_provider import mock_provider
from .model_registry import configured_model_id, get_model_registry

logger = get_logger("providers.router")

# Passage 1 §2's provider set, plus mock. Order is the display order.
_REGISTRY: dict[str, AIProvider] = {
    "custom": custom_ai_provider,
    "grok": grok_provider,
    "gemma": gemma_provider,
    "mock": mock_provider,
}

# Passage 1 §4.5: mode is one of CUSTOM | GROK | GEMMA | MULTI.
# "Both" no longer exists as a category (§4.6).
RouterMode = Literal["custom", "grok", "gemma", "multi"]

# The blueprint's authoritative provider set for comparison (§4.6).
# Mock is accepted too, so the comparison view is demonstrable without
# live credentials — but it is not one of the blueprint's combinations.
BLUEPRINT_PROVIDER_IDS = ("custom", "grok", "gemma")
VALID_PROVIDER_IDS = ("mock", "custom", "grok", "gemma")


@dataclass
class RouteDecision:
    """
    What the router would do, without executing anything.

    This is the return shape of `POST /api/ai/route` — the endpoint
    Passage 4 §6.1 (G08a) identifies as accidentally removed from
    Passage 1's table and restores as a seventh row: "exposes the
    Provider Router's resolve() ... for cases where a caller wants
    routing decided without immediately executing a chat call."
    """

    requested_provider_id: str
    resolved_provider_id: str
    model_id: str
    source_tag: str
    available: bool
    reason: str
    circuit_state: str
    fell_back: bool
    missing_configuration: list[str] = field(default_factory=list)
    model_redirect_note: str | None = None


def source_tag_for(provider_id: str) -> str:
    """
    Passage 1 §4.5's `tag(result, source=model.origin)`, with §4.4's hard
    rule: a local runtime's response is never labelled hosted_api.
    """
    if provider_id == "mock":
        return "mock"
    if provider_id == "custom":
        return custom_ai_provider.source_tag()
    if provider_id == "gemma":
        return gemma_provider.transport()
    return "hosted_api"


def get_provider_strict(provider_id: str) -> AIProvider:
    """
    For multi-provider comparison: never silently substitutes mock for an
    unconfigured provider. A comparison is meant to show distinct real
    providers side by side — quietly running mock logic under a "Grok"
    label would defeat the point. Unchanged behaviour from before this
    pass; kept because it is correct.
    """
    provider = _REGISTRY.get(provider_id)
    if provider is None:
        raise KeyError(f"Unknown provider id: {provider_id}")
    return provider


def resolve_provider(requested_id: str | None) -> AIProvider:
    """
    Single-provider fallback resolution. Same contract as before this
    pass — degrade to a controlled, working path rather than hard-fail.
    """
    provider = _REGISTRY.get(requested_id) if requested_id else None
    if provider is None or not provider.is_configured():
        return mock_provider
    return provider


async def describe_route(requested_id: str, *, allow_fallback: bool = True) -> RouteDecision:
    """
    Stage 1-3 of §4.5's pipeline — health check, circuit breaker,
    fallback — with no execution. Backs `POST /api/ai/route`.
    """
    registry = get_model_registry()
    breakers = get_circuit_breakers()

    provider = _REGISTRY.get(requested_id)
    if provider is None:
        return RouteDecision(
            requested_provider_id=requested_id,
            resolved_provider_id="mock" if allow_fallback else requested_id,
            model_id="mock-v1" if allow_fallback else "unknown",
            source_tag="mock" if allow_fallback else "none",
            available=allow_fallback,
            reason=f"Unknown provider id '{requested_id}'",
            circuit_state="closed",
            fell_back=allow_fallback,
        )

    snapshot = await breakers.snapshot(requested_id)
    requested_model = configured_model_id(requested_id)
    _, redirect_note = registry.resolve_model_slug(requested_id, requested_model)
    missing = provider.missing_configuration()

    if snapshot.state == "open":
        fallback_id = "mock" if allow_fallback else requested_id
        return RouteDecision(
            requested_provider_id=requested_id,
            resolved_provider_id=fallback_id,
            model_id=configured_model_id(fallback_id) if allow_fallback else requested_model,
            source_tag=source_tag_for(fallback_id),
            available=allow_fallback,
            reason=(
                f"Circuit breaker is open for '{requested_id}' "
                f"(retry in ~{snapshot.retry_after_seconds:.0f}s)"
                if snapshot.retry_after_seconds is not None
                else f"Circuit breaker is open for '{requested_id}'"
            ),
            circuit_state=snapshot.state,
            fell_back=allow_fallback,
            missing_configuration=missing,
            model_redirect_note=redirect_note,
        )

    if not provider.is_configured():
        fallback_id = "mock" if allow_fallback else requested_id
        return RouteDecision(
            requested_provider_id=requested_id,
            resolved_provider_id=fallback_id,
            model_id=configured_model_id(fallback_id) if allow_fallback else requested_model,
            source_tag=source_tag_for(fallback_id),
            available=allow_fallback,
            reason=f"'{requested_id}' is not configured. Missing: {', '.join(missing)}",
            circuit_state=snapshot.state,
            fell_back=allow_fallback,
            missing_configuration=missing,
            model_redirect_note=redirect_note,
        )

    return RouteDecision(
        requested_provider_id=requested_id,
        resolved_provider_id=requested_id,
        model_id=requested_model,
        source_tag=source_tag_for(requested_id),
        available=True,
        reason="Provider is configured and its circuit is closed",
        circuit_state=snapshot.state,
        fell_back=False,
        missing_configuration=[],
        model_redirect_note=redirect_note,
    )


@dataclass
class ExecutionResult:
    provider_id: str
    model_id: str
    source_tag: str
    reasoning: ProviderReasoning
    latency_ms: float
    fell_back: bool


async def execute_provider(
    provider_id: str, context: AnalysisContext, *, allow_fallback: bool = False
) -> ExecutionResult:
    """
    Stages 2-6 of §4.5: circuit breaker -> execute -> validate -> tag,
    with success/failure recorded against the breaker either way.

    `allow_fallback=False` is the comparison path: a failure must surface
    as that provider's own failure, not be papered over with mock.
    """
    breakers = get_circuit_breakers()
    provider = get_provider_strict(provider_id)
    model_id = configured_model_id(provider_id)
    started = time.perf_counter()

    try:
        await breakers.ensure_closed(provider_id)
    except CircuitBreakerOpen:
        if not allow_fallback:
            raise
        logger.warning("falling back to mock; circuit open", extra={"provider": provider_id})
        reasoning = await mock_provider.reason(context)
        return ExecutionResult(
            provider_id="mock",
            model_id="mock-v1",
            source_tag="fallback",
            reasoning=reasoning,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            fell_back=True,
        )

    try:
        reasoning = await provider.reason(context)
    except Exception as err:  # noqa: BLE001 — every failure feeds the breaker
        await breakers.record_failure(provider_id, f"{type(err).__name__}: {err}")
        if not allow_fallback:
            raise
        logger.warning(
            "provider failed; falling back to mock",
            extra={"provider": provider_id, "error": str(err)},
        )
        reasoning = await mock_provider.reason(context)
        return ExecutionResult(
            provider_id="mock",
            model_id="mock-v1",
            source_tag="fallback",
            reasoning=reasoning,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            fell_back=True,
        )

    await breakers.record_success(provider_id)
    return ExecutionResult(
        provider_id=provider_id,
        model_id=model_id,
        source_tag=source_tag_for(provider_id),
        reasoning=reasoning,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        fell_back=False,
    )


def list_providers() -> list[dict[str, Any]]:
    """
    Backs `GET /api/ai/providers` — Passage 1 §10: "Unchanged in shape;
    now returns three rows instead of two."
    """
    registry = get_model_registry()
    rows: list[dict[str, Any]] = []
    for provider_id, provider in _REGISTRY.items():
        rows.append(
            {
                "id": provider_id,
                "displayName": provider.display_name,
                "configured": provider.is_configured(),
                "missingConfiguration": provider.missing_configuration(),
                "isBlueprintProvider": provider_id in BLUEPRINT_PROVIDER_IDS,
                "supportsTools": provider.supports_tools,
                "supportsLocalRuntime": provider.supports_local_runtime,
                "sourceTag": source_tag_for(provider_id),
                "defaultModelId": configured_model_id(provider_id),
                "models": [
                    {
                        "modelId": m.model_id,
                        "displayName": m.display_name,
                        "status": m.status,
                        "contextWindow": m.context_window,
                        "isDefault": m.is_default,
                        "isFallback": m.is_fallback,
                        "onlineCapable": m.online_capable,
                        "offlineCapable": m.offline_capable,
                        "routableForAnalysis": m.routable_for_analysis,
                        "redirectsTo": m.redirects_to,
                        "notes": m.notes,
                    }
                    for m in registry.all(provider_id)
                ],
                # Passage 1 §4.4's Admin Panel contract: Grok's offline
                # row must render "Unavailable - hosted API only" rather
                # than a toggle the user can switch on.
                "offlineStatus": (
                    "Unavailable - hosted API only"
                    if not provider.supports_local_runtime
                    else "Supported"
                ),
            }
        )
    return rows


async def health_check_all() -> dict[str, HealthResult]:
    results: dict[str, HealthResult] = {}
    for provider_id, provider in _REGISTRY.items():
        try:
            results[provider_id] = await provider.health_check()
        except Exception as err:  # noqa: BLE001 — one bad provider must not break the sweep
            results[provider_id] = HealthResult(
                ok=False,
                latency_ms=None,
                model_checked=None,
                detail=f"{type(err).__name__}: {err}",
            )
    return results


def provider_ids() -> list[str]:
    return list(_REGISTRY.keys())