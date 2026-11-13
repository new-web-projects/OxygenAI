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

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from ..observability import get_logger
from .base import (
    AIProvider,
    AnalysisContext,
    HealthResult,
    ProviderDisabledError,
    ProviderNoModelError,
    ProviderNotConfiguredError,
    ProviderReasoning,
)
from .circuit_breaker import CircuitBreakerOpen, get_circuit_breakers
from .custom_ai_provider import custom_ai_provider
from .gemma_provider import gemma_provider
from .grok_provider import grok_provider
from .mock_provider import mock_provider
from .model_health import get_model_health
from .model_registry import configured_model_id, get_model_registry, model_selection_source
from .provider_settings import is_provider_enabled, provider_priority

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


def provider_unavailability_reason(requested_id: str | None) -> str | None:
    """
    Why `resolve_provider(requested_id)` would hand back mock instead of the requested provider
    (None when it would hand back the requested one). Lets the caller TELL the user — before P3 a
    request for an unconfigured Grok silently came back from mock and only the `provider` field
    revealed it.
    """
    if not requested_id:
        return None
    provider = _REGISTRY.get(requested_id)
    if provider is None:
        return f"Unknown provider id '{requested_id}'"
    if not is_provider_enabled(requested_id):
        return f"'{requested_id}' has been disabled by an administrator"
    if not provider.is_configured():
        return f"'{requested_id}' is not configured. Missing: {', '.join(provider.missing_configuration())}"
    return None


def resolve_provider(requested_id: str | None) -> AIProvider:
    """
    Single-provider fallback resolution. Same contract as before this
    pass — degrade to a controlled, working path rather than hard-fail.
    """
    if requested_id and provider_unavailability_reason(requested_id) is None:
        return _REGISTRY[requested_id]
    return mock_provider


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
    selection = select_call_model(requested_id)
    requested_model = selection.model_id or "unset"
    _, redirect_note = registry.resolve_model_slug(requested_id, requested_model)
    redirect_note = "; ".join(n for n in (redirect_note, selection.note) if n) or None
    missing = provider.missing_configuration()

    if not is_provider_enabled(requested_id):
        fallback_id = "mock" if allow_fallback else requested_id
        return RouteDecision(
            requested_provider_id=requested_id,
            resolved_provider_id=fallback_id,
            model_id=configured_model_id(fallback_id) if allow_fallback else requested_model,
            source_tag=source_tag_for(fallback_id),
            available=allow_fallback,
            reason=f"'{requested_id}' has been disabled by an administrator",
            circuit_state=snapshot.state,
            fell_back=allow_fallback,
            missing_configuration=missing,
            model_redirect_note=redirect_note,
        )

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

    if selection.model_id is None:
        fallback_id = "mock" if allow_fallback else requested_id
        return RouteDecision(
            requested_provider_id=requested_id,
            resolved_provider_id=fallback_id,
            model_id=configured_model_id(fallback_id) if allow_fallback else requested_model,
            source_tag=source_tag_for(fallback_id),
            available=allow_fallback,
            reason=str(ProviderNoModelError(requested_id)),
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
    # Why a different provider answered than the one requested (set whenever fell_back).
    fallback_reason: str | None = None
    # Set when the provider's fallback MODEL answered instead of its default model.
    model_note: str | None = None


@dataclass
class ModelSelection:
    model_id: str | None
    substituted: bool = False
    note: str | None = None


def select_call_model(provider_id: str) -> ModelSelection:
    """
    The "health check -> fallback model" stage of Passage 1 §4.5:

        if not provider.health_ok():
            model = registry.get_fallback(provider) if provider.has_configured_fallback() else None

    Health here is what the system has already observed about the model (providers/model_health.py),
    not a live probe per request. If the default model is flagged unhealthy and the provider has a
    healthy fallback model, that model is used and the substitution is reported. If it has none, the
    default is STILL attempted: the blueprint's pseudocode returns "unavailable" at this point, but
    with a remembered (not freshly probed) health flag that would lock the model out with no way to
    recover; the circuit breaker is the mechanism that short-circuits a failing provider, and it
    does so with a cooldown and a trial call.
    """
    default_id = configured_model_id(provider_id)
    if default_id in ("", "unset"):
        return ModelSelection(None, note="no model configured")
    health = get_model_health()
    if health.is_healthy(provider_id, default_id):
        return ModelSelection(default_id)
    registry = get_model_registry()
    fallback = registry.fallback_for(provider_id, excluding=default_id)
    if fallback:
        effective, _ = registry.resolve_model_slug(provider_id, fallback)
        if effective != default_id and health.is_healthy(provider_id, effective):
            detail = health.detail(provider_id, default_id)
            return ModelSelection(
                effective,
                substituted=True,
                note=(
                    f"Default model '{default_id}' is unhealthy"
                    + (f" ({detail})" if detail else "")
                    + f"; answered by the provider's fallback model '{effective}'."
                ),
            )
    return ModelSelection(
        default_id,
        note=f"Default model '{default_id}' is flagged unhealthy and no healthy fallback model is configured; attempting it anyway.",
    )


def _mock_fallback(
    reasoning: ProviderReasoning, started: float, reason: str
) -> ExecutionResult:
    return ExecutionResult(
        provider_id="mock",
        model_id="mock-v1",
        source_tag="fallback",
        reasoning=reasoning,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        fell_back=True,
        fallback_reason=reason,
    )


async def execute_provider(
    provider_id: str, context: AnalysisContext, *, allow_fallback: bool = False
) -> ExecutionResult:
    """
    Stages 2-6 of §4.5: circuit breaker -> execute -> validate -> tag,
    with success/failure recorded against the breaker either way.

    `allow_fallback=False` is the comparison path: a failure must surface
    as that provider's own failure, not be papered over with mock.

    States that are not outages — a provider that is not configured, one an administrator
    disabled, one with no callable model — are neither called nor recorded against the breaker:
    with `allow_fallback` they degrade to mock exactly like any other unavailability, without it
    a specific error (`ProviderNotConfiguredError`, `ProviderDisabledError`,
    `ProviderNoModelError`) carries the real reason.
    """
    breakers = get_circuit_breakers()
    provider = get_provider_strict(provider_id)
    started = time.perf_counter()

    # --- states, not failures: decided before the breaker is touched -----------------------
    state_error: Exception | None = None
    if not is_provider_enabled(provider_id):
        state_error = ProviderDisabledError(provider_id)
    elif not provider.is_configured():
        state_error = ProviderNotConfiguredError(provider_id, provider.missing_configuration())
    selection = ModelSelection(None) if state_error else select_call_model(provider_id)
    if state_error is None and selection.model_id is None:
        state_error = ProviderNoModelError(provider_id)
    if state_error is not None:
        if not allow_fallback:
            raise state_error
        logger.warning(
            "falling back to mock",
            extra={"provider": provider_id, "reason": str(state_error)},
        )
        return _mock_fallback(await mock_provider.reason(context), started, str(state_error))

    model_id = selection.model_id
    assert model_id is not None

    try:
        await breakers.ensure_closed(provider_id)
    except CircuitBreakerOpen as open_err:
        if not allow_fallback:
            raise
        logger.warning("falling back to mock; circuit open", extra={"provider": provider_id})
        return _mock_fallback(await mock_provider.reason(context), started, str(open_err))

    health = get_model_health()
    try:
        # `model_id=` is passed only when the fallback MODEL is being substituted; a normal call
        # is exactly `reason(context)` and the provider selects its own configured model.
        if selection.substituted:
            reasoning = await provider.reason(context, model_id=model_id)
        else:
            reasoning = await provider.reason(context)
    except asyncio.CancelledError:
        # A cancelled request (client gone, upstream timeout) says nothing about the provider's
        # health. If this call held the half-open trial slot, give it back — otherwise the
        # provider stays locked out until the process restarts.
        await breakers.release_trial(provider_id)
        raise
    except Exception as err:  # noqa: BLE001 — every failure feeds the breaker
        health.record_failure(provider_id, model_id, f"{type(err).__name__}: {err}")
        await breakers.record_failure(provider_id, f"{type(err).__name__}: {err}")
        if not allow_fallback:
            raise
        logger.warning(
            "provider failed; falling back to mock",
            extra={"provider": provider_id, "error": str(err)},
        )
        return _mock_fallback(await mock_provider.reason(context), started, f"{type(err).__name__}: {err}")

    health.record_success(provider_id, model_id)
    await breakers.record_success(provider_id)
    return ExecutionResult(
        provider_id=provider_id,
        model_id=model_id,
        source_tag=source_tag_for(provider_id),
        reasoning=reasoning,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        fell_back=False,
        model_note=selection.note if selection.substituted else None,
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
                "fallbackModelId": registry.fallback_model_id(provider_id),
                # Where the model comes from: an environment variable wins whenever it is set,
                # otherwise the admin-managed registry. Shown so an admin can see why changing
                # the default in the registry has no effect while the variable is set.
                "modelSelectionSource": model_selection_source(provider_id),
                "enabled": is_provider_enabled(provider_id),
                "priority": provider_priority(provider_id),
                "models": [
                    {
                        "id": m.db_id,
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