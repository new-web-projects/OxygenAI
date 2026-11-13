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

Administration added in P3 (all admin-only, all audited):

* PATCH /api/ai/providers/{id}            — enable/disable a provider, set its priority.
* GET   /api/ai/providers/{id}/settings   — effective settings and where each value comes from
                                            (never the API key, only whether one is stored).
* PATCH /api/ai/providers/{id}/settings   — admin overrides: API key (write-only, encrypted at
                                            rest), base URL, timeout, retries, region.
* GET/PUT/DELETE /api/ai/circuit-breaker[/settings], POST /api/ai/circuit-breaker/{id}/reset
                                          — Passage 4 §3.3 (G03): "5 failures / 60 seconds,
                                            admin-configurable", live state, manual reset.

Auth (this pass): `/route` requires any authenticated user;
`/{id}/test` and `/{id}/health` require the admin role, per Passage 4
§6.2's explicit "Both admin-only." `GET /providers` stays open — it
returns configuration booleans and catalogue data (never a credential),
and Passage 1 §10 doesn't mark it admin-only; the live frontend also
never calls any endpoint in this router, so none of this changes
existing behaviour for anyone using the product today.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends

from ..db.audit import record_audit
from ..db.health import record_health_check
from ..db.provider_settings import (
    get_provider_settings,
    update_provider_flags,
    upsert_provider_settings,
)
from ..db.system_settings import delete_setting, get_setting, set_setting
from ..errors import ConfigurationError, NotFoundError, ValidationError
from ..providers.circuit_breaker import get_circuit_breakers
from ..providers.model_health import get_model_health
from ..providers.provider_settings import (
    api_key_source,
    base_url_source,
    effective_base_url,
    effective_policy,
    get_overrides,
    is_provider_enabled,
    provider_priority,
)
from ..providers.registry import (
    VALID_PROVIDER_IDS,
    describe_route,
    get_provider_strict,
    list_providers,
    provider_ids,
)
from ..providers.runtime_config import (
    BREAKER_KEY,
    parse_breaker_config,
    reload_breaker_settings,
    reload_provider_settings,
)
from ..schemas import (
    BreakerSettingsRequest,
    HealthResponse,
    ProviderPatchRequest,
    ProviderSettingsPatchRequest,
    RouteDecisionResponse,
    RouteRequest,
    RouteResponse,
)
from ..security import secret_store
from ..security.dependencies import AuthenticatedUser, get_current_user, require_role

router = APIRouter(prefix="/api/ai", tags=["providers"])


@router.get("/providers")
async def get_providers() -> list[dict]:
    return list_providers()


@router.post("/route", dependencies=[Depends(get_current_user)])
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


@router.post("/{provider_id}/test", dependencies=[Depends(require_role("admin"))])
async def test_provider(provider_id: str) -> HealthResponse:
    if provider_id not in provider_ids():
        raise NotFoundError(f"Unknown provider id: {provider_id}")
    provider = get_provider_strict(provider_id)
    result = await provider.health_check()
    # What a probe learns about a model feeds the router's health stage (providers/model_health.py).
    # An unconfigured provider says nothing about its model, so it is not recorded.
    if provider.is_configured() and result.model_checked not in (None, "", "unset"):
        get_model_health().record_probe(provider_id, str(result.model_checked), result.ok, result.detail)
    # Passage 1 §9.1: probe history is persisted for the monitoring dashboard.
    # Best-effort — the probe result below is returned either way.
    await record_health_check(
        provider_id, result.model_checked, result.ok, result.latency_ms, result.detail
    )
    return HealthResponse(
        ok=result.ok,
        latencyMs=result.latency_ms,
        modelChecked=result.model_checked,
        detail=result.detail,
        transport=result.transport,
    )


@router.get("/{provider_id}/health", dependencies=[Depends(require_role("admin"))])
async def provider_health(provider_id: str) -> HealthResponse:
    return await test_provider(provider_id)


# ---------------------------------------------------------------------------
# P3 — provider administration
# ---------------------------------------------------------------------------

_API_KEY_ENV = {"grok": "XAI_API_KEY", "gemma": "GOOGLE_AI_API_KEY", "custom": "CUSTOM_AI_API_KEY"}
_BASE_URL_ENV = {"grok": None, "gemma": "GEMMA_LOCAL_BASE_URL", "custom": "CUSTOM_AI_BASE_URL"}
_BASE_URL_DEFAULT = {"grok": "https://api.x.ai/v1", "gemma": "http://localhost:11434/v1", "custom": ""}


def _administrable(provider_id: str) -> str:
    if provider_id not in VALID_PROVIDER_IDS:
        raise NotFoundError(f"Unknown provider id: {provider_id}")
    if provider_id == "mock":
        raise ValidationError("The offline mock provider has no settings to administer")
    return provider_id


def _clean_base_url(provider_id: str, raw: str) -> str:
    parsed = urlparse(raw.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError("baseUrl must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValidationError("baseUrl must not embed credentials; use the API key setting")
    if parsed.query or parsed.fragment:
        raise ValidationError("baseUrl must not contain a query string or fragment")
    if provider_id == "grok" and parsed.scheme != "https":
        # The xAI key is sent to this URL; a hosted vendor API is never reached over plain http.
        raise ValidationError("Grok's baseUrl must use https")
    return raw.strip().rstrip("/")


def _settings_view(provider_id: str, row=None) -> dict:
    """What is in effect for a provider and where each value comes from. Never contains a key."""
    ovr = get_overrides(provider_id)
    policy = effective_policy(provider_id)
    env_key = _API_KEY_ENV.get(provider_id)
    return {
        "providerId": provider_id,
        "enabled": is_provider_enabled(provider_id),
        "priority": provider_priority(provider_id),
        "apiKey": {
            "stored": ovr.key_stored,
            "source": api_key_source(provider_id, env_key) if env_key else "none",
            # Set when a stored key cannot be decrypted; the provider is then using its environment.
            "error": ovr.key_error,
        },
        "baseUrl": {
            "override": ovr.base_url,
            "effective": effective_base_url(provider_id, _BASE_URL_ENV.get(provider_id), _BASE_URL_DEFAULT.get(provider_id, "")),
            "source": base_url_source(provider_id, _BASE_URL_ENV.get(provider_id)),
        },
        "timeoutMs": {"override": ovr.timeout_ms, "effective": round(policy.timeout_seconds * 1000)},
        "retryCount": {"override": ovr.retry_count, "effective": policy.max_attempts - 1},
        "region": ovr.region,  # stored only; no provider consumes it yet (see providers/provider_settings.py)
        "secretStoreConfigured": secret_store.is_configured(),
        "updatedAt": row.updated_at.isoformat() if row and row.updated_at else None,
        "updatedBy": row.updated_by if row else None,
    }


def _audit_settings_state(row) -> dict:
    """The audit-safe projection of a settings row: ciphertext is never written to audit_logs."""
    if row is None:
        return {}
    return {
        "apiKeySet": bool(row.api_key_encrypted),
        "baseUrl": row.base_url,
        "timeoutMs": row.timeout_ms,
        "retryCount": row.retry_count,
        "region": row.region,
    }


@router.patch("/providers/{provider_id}")
async def patch_provider(
    provider_id: str,
    body: ProviderPatchRequest,
    admin: AuthenticatedUser = Depends(require_role("admin")),
) -> dict:
    _administrable(provider_id)
    if not body.model_fields_set:
        raise ValidationError("No fields were provided")
    before, after = await update_provider_flags(
        provider_id, enabled=body.enabled, priority=body.priority
    )
    await record_audit(
        actor_id=admin.id,
        action="provider.update",
        resource=f"ai_provider:{provider_id}",
        before=before,
        after=after,
    )
    await reload_provider_settings()
    return {"providerId": provider_id, **after}


@router.get("/providers/{provider_id}/settings")
async def get_provider_settings_view(
    provider_id: str, _admin: AuthenticatedUser = Depends(require_role("admin"))
) -> dict:
    _administrable(provider_id)
    return _settings_view(provider_id, await get_provider_settings(provider_id))


@router.patch("/providers/{provider_id}/settings")
async def patch_provider_settings(
    provider_id: str,
    body: ProviderSettingsPatchRequest,
    admin: AuthenticatedUser = Depends(require_role("admin")),
) -> dict:
    _administrable(provider_id)
    provided = body.model_fields_set
    if not provided:
        raise ValidationError("No fields were provided")

    updates: dict = {}
    if "apiKey" in provided:
        if body.apiKey is None:
            updates["api_key_encrypted"] = None  # clear the override; the environment key applies again
        else:
            key = body.apiKey.strip()
            if len(key) < 8:
                raise ValidationError("apiKey is too short")
            try:
                updates["api_key_encrypted"] = secret_store.encrypt_secret(key)
            except secret_store.SecretStoreError as err:
                # Never fall back to storing plaintext.
                raise ConfigurationError(str(err)) from err
    if "baseUrl" in provided:
        updates["base_url"] = None if body.baseUrl is None else _clean_base_url(provider_id, body.baseUrl)
    if "timeoutMs" in provided:
        updates["timeout_ms"] = body.timeoutMs
    if "retryCount" in provided:
        updates["retry_count"] = body.retryCount
    if "region" in provided:
        updates["region"] = (body.region or "").strip() or None

    before, after = await upsert_provider_settings(provider_id, admin.id, **updates)
    await record_audit(
        actor_id=admin.id,
        action="provider.settings_update",
        resource=f"ai_provider:{provider_id}",
        before=_audit_settings_state(before),
        after={**_audit_settings_state(after), "apiKeyChanged": "api_key_encrypted" in updates},
    )
    await reload_provider_settings()
    return _settings_view(provider_id, after)


async def _breaker_view() -> dict:
    breakers = get_circuit_breakers()
    stored = await get_setting(BREAKER_KEY)
    default, per_provider = parse_breaker_config(stored)
    snapshots = [await breakers.snapshot(pid) for pid in VALID_PROVIDER_IDS]
    return {
        "source": "database" if stored is not None else "environment",
        "defaults": {
            "failureThreshold": default.failure_threshold,
            "windowSeconds": default.window_seconds,
            "cooldownSeconds": default.cooldown_seconds,
        },
        "providers": {
            name: {
                "failureThreshold": s.failure_threshold,
                "windowSeconds": s.window_seconds,
                "cooldownSeconds": s.cooldown_seconds,
            }
            for name, s in per_provider.items()
        },
        "breakers": [
            {
                "providerId": s.provider_id,
                "state": s.state,
                "recentFailures": s.recent_failures,
                "failureThreshold": s.failure_threshold,
                "windowSeconds": s.window_seconds,
                "cooldownSeconds": s.cooldown_seconds,
                "retryAfterSeconds": s.retry_after_seconds,
                "lastError": s.last_error,
                "totalFailures": s.total_failures,
                "totalSuccesses": s.total_successes,
                "totalShortCircuits": s.total_short_circuits,
            }
            for s in snapshots
        ],
    }


@router.get("/circuit-breaker")
async def get_circuit_breaker(_admin: AuthenticatedUser = Depends(require_role("admin"))) -> dict:
    return await _breaker_view()


@router.put("/circuit-breaker/settings")
async def put_circuit_breaker_settings(
    body: BreakerSettingsRequest, admin: AuthenticatedUser = Depends(require_role("admin"))
) -> dict:
    """Replaces the stored thresholds (omitted fields = the environment defaults)."""
    value = body.model_dump(exclude_none=True, exclude={"providers"})
    if body.providers:
        unknown = sorted(set(body.providers) - set(VALID_PROVIDER_IDS))
        if unknown:
            raise ValidationError(f"Unknown provider id(s): {', '.join(unknown)}")
        value["providers"] = {
            name: limits.model_dump(exclude_none=True) for name, limits in body.providers.items()
        }
    previous = await set_setting(BREAKER_KEY, value, admin.id)
    await record_audit(
        actor_id=admin.id,
        action="circuit_breaker.settings_update",
        resource=f"system_setting:{BREAKER_KEY}",
        before=previous,
        after=value,
    )
    await reload_breaker_settings()
    return await _breaker_view()


@router.delete("/circuit-breaker/settings")
async def delete_circuit_breaker_settings(
    admin: AuthenticatedUser = Depends(require_role("admin")),
) -> dict:
    """Removes the admin overrides; the environment defaults (CB_*) apply again."""
    previous = await get_setting(BREAKER_KEY)
    await delete_setting(BREAKER_KEY)
    await record_audit(
        actor_id=admin.id,
        action="circuit_breaker.settings_reset",
        resource=f"system_setting:{BREAKER_KEY}",
        before=previous,
    )
    await reload_breaker_settings()
    return await _breaker_view()


@router.post("/circuit-breaker/{provider_id}/reset")
async def reset_circuit_breaker(
    provider_id: str, admin: AuthenticatedUser = Depends(require_role("admin"))
) -> dict:
    if provider_id not in VALID_PROVIDER_IDS:
        raise NotFoundError(f"Unknown provider id: {provider_id}")
    breakers = get_circuit_breakers()
    before = await breakers.snapshot(provider_id)
    await breakers.reset(provider_id)
    get_model_health().reset(provider_id)
    await record_audit(
        actor_id=admin.id,
        action="circuit_breaker.reset",
        resource=f"ai_provider:{provider_id}",
        before={"state": before.state, "recentFailures": before.recent_failures},
        after={"state": "closed"},
    )
    return {"providerId": provider_id, "state": "closed"}
