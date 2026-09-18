"""
Provider/model administrative endpoints.

* GET  /api/ai/providers        — Passage 1 §10: "Unchanged in shape;
                                    now returns three rows instead of two."
* POST /api/ai/route             — Passage 4 §6.1 (G08a): restores the
                                    endpoint Passage 1's table dropped.
                                    Exposes resolve() without executing a
                                    chat call.
* POST /api/ai/{id}/test         — Passage 1 §10: "Mirrors
                                    /api/ai/grok/test — verifies stored
                                    credentials ... without exposing
                                    them." Generalised to every provider
                                    id rather than duplicated per
                                    provider, matching the "already
                                    generic over provider_id" pattern
                                    Passage 1 itself uses for models CRUD.
* GET  /api/ai/{id}/health       — Passage 1 §10's gemma/grok health,
                                    generalised the same way. Reports
                                    both hosted and local-runtime health
                                    where a local runtime is configured
                                    (§4.4).

Passage 4 §6.2 marks the test/health endpoints admin-only. Auth/RBAC is
not yet wired into this service (tracked separately — see the delivery
report), so these are open for now, same as every other endpoint. Not
silently claiming an RBAC boundary that does not exist yet.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..errors import NotFoundError
from ..providers.registry import (
    describe_route,
    list_providers,
    provider_ids,
)
from ..schemas import HealthResponse, RouteDecisionResponse, RouteRequest, RouteResponse

router = APIRouter(prefix="/api/ai", tags=["providers"])


@router.get("/providers")
async def get_providers() -> list[dict]:
    return list_providers()


@router.post("/route")
async def route(body: RouteRequest) -> RouteResponse:
    """
    Passage 4 §6.1 (G08a): "exposes the Provider Router's resolve() ...
    for cases where a caller wants routing decided without immediately
    executing a chat call."
    """
    if body.providers:
        ids = body.providers
    elif body.mode and body.mode != "multi":
        ids = [body.mode]
    elif body.provider:
        ids = [body.provider]
    else:
        ids = ["mock"]

    decisions = []
    for provider_id in ids:
        decision = await describe_route(provider_id, allow_fallback=body.allowFallback)
        decisions.append(
            RouteDecisionResponse(
                requestedProviderId=decision.requested_provider_id,
                resolvedProviderId=decision.resolved_provider_id,
                modelId=decision.model_id,
                sourceTag=decision.source_tag,
                available=decision.available,
                reason=decision.reason,
                circuitState=decision.circuit_state,
                fellBack=decision.fell_back,
                missingConfiguration=decision.missing_configuration,
                modelRedirectNote=decision.model_redirect_note,
            )
        )

    return RouteResponse(mode="multi" if len(ids) > 1 else ids[0], decisions=decisions)


@router.post("/{provider_id}/test")
async def test_provider(provider_id: str) -> HealthResponse:
    if provider_id not in provider_ids():
        raise NotFoundError(f"Unknown provider id: {provider_id}")
    from ..providers.registry import get_provider_strict

    result = await get_provider_strict(provider_id).health_check()
    return HealthResponse(
        ok=result.ok,
        latencyMs=result.latency_ms,
        modelChecked=result.model_checked,
        detail=result.detail,
        transport=result.transport,
    )


@router.get("/{provider_id}/health")
async def provider_health(provider_id: str) -> HealthResponse:
    return await test_provider(provider_id)