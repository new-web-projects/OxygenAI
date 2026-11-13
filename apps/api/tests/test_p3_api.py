"""
Phase P3 — the administration API, end to end over HTTP against a real Postgres: Models CRUD,
provider settings (encrypted API keys), provider enable/disable, circuit-breaker configuration,
`POST /api/ai/compare`, the single-mode substitution signal, the availability poll and the
catalogue sync. Skipped without DATABASE_URL. State changed here is restored by `p3_admin_state`.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL is not set — P3 API tests require a live Postgres instance",
)

PROVIDER_ENV = (
    "XAI_API_KEY", "GOOGLE_AI_API_KEY", "GEMMA_TRANSPORT", "GEMMA_LOCAL_BASE_URL", "GROK_MODEL_ID",
    "GEMMA_MODEL_ID", "CUSTOM_AI_BASE_URL", "CUSTOM_AI_MODEL_ID", "CUSTOM_AI_API_KEY", "SETTINGS_ENCRYPTION_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)


def _mid() -> str:
    return f"zzp3-{uuid.uuid4().hex[:8]}"


async def _drop_instrument(pool, symbol: str) -> None:
    for table in ("trading_signals", "technical_indicators", "market_data"):
        await pool.execute(
            f"DELETE FROM {table} WHERE instrument_id IN (SELECT id FROM market_instruments WHERE symbol = $1)", symbol
        )
    await pool.execute("DELETE FROM market_instruments WHERE symbol = $1", symbol)


async def _audit(pool, actor_id: str, action: str | None = None) -> list:
    if action:
        return await pool.fetch(
            "SELECT action, resource, before, after FROM audit_logs WHERE actor_id = $1 AND action = $2 ORDER BY ts", actor_id, action
        )
    return await pool.fetch("SELECT action, resource, before, after FROM audit_logs WHERE actor_id = $1 ORDER BY ts", actor_id)


# --------------------------------------------------------------------------
# Authorization matrix
# --------------------------------------------------------------------------

ADMIN_ONLY = [
    ("GET", "/api/ai/models", None),
    ("POST", "/api/ai/models", {"providerId": "custom", "modelId": "zzp3-x", "displayName": "X"}),
    ("GET", "/api/ai/models/{uuid}", None),
    ("PATCH", "/api/ai/models/{uuid}", {"displayName": "Y"}),
    ("DELETE", "/api/ai/models/{uuid}", None),
    ("POST", "/api/ai/models/sync-catalogue", None),
    ("POST", "/api/ai/models/availability-check", None),
    ("PATCH", "/api/ai/providers/grok", {"enabled": True}),
    ("GET", "/api/ai/providers/grok/settings", None),
    ("PATCH", "/api/ai/providers/grok/settings", {"timeoutMs": 2000}),
    ("GET", "/api/ai/circuit-breaker", None),
    ("PUT", "/api/ai/circuit-breaker/settings", {}),
    ("DELETE", "/api/ai/circuit-breaker/settings", None),
    ("POST", "/api/ai/circuit-breaker/grok/reset", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", ADMIN_ONLY, ids=[f"{m} {p}" for m, p, _ in ADMIN_ONLY])
async def test_every_administration_endpoint_is_admin_only(p3_client, p3_accounts, p3_admin_state, method, path, body):
    path = path.replace("{uuid}", str(uuid.uuid4()))
    _, user_headers = await p3_accounts(p3_client, role="user")
    anonymous = await p3_client.request(method, path, json=body)
    as_user = await p3_client.request(method, path, json=body, headers=user_headers)
    assert anonymous.status_code == 401, anonymous.text
    assert as_user.status_code == 403, as_user.text


# --------------------------------------------------------------------------
# Models CRUD
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_models_crud_flow_is_validated_audited_and_reflected_in_the_registry(p3_client, p3_accounts, p3_admin_state, p3_pool):
    admin_id, admin = await p3_accounts(p3_client, role="admin")
    first_id, second_id = _mid(), _mid()

    created = await p3_client.post("/api/ai/models", headers=admin, json={
        "providerId": "custom", "modelId": first_id, "displayName": "First", "contextWindow": 8192,
        "isDefault": True, "inputModalities": ["text"], "config": {"note": "a"}})
    assert created.status_code == 201, created.text
    first = created.json()
    assert (first["providerId"], first["status"], first["isDefault"], first["contextWindow"]) == ("custom", "active", True, 8192)

    second = (await p3_client.post("/api/ai/models", headers=admin, json={
        "providerId": "custom", "modelId": second_id, "displayName": "Second", "status": "fallback", "isFallback": True})).json()

    listed = (await p3_client.get("/api/ai/models", params={"provider": "custom"}, headers=admin)).json()
    assert {m["id"] for m in listed} >= {first["id"], second["id"]} and all(m["providerId"] == "custom" for m in listed)
    assert (await p3_client.get(f"/api/ai/models/{first['id']}", headers=admin)).json()["displayName"] == "First"

    # the registry is live: the open providers list shows the admin's default for Custom AI
    custom = next(p for p in (await p3_client.get("/api/ai/providers")).json() if p["id"] == "custom")
    assert custom["defaultModelId"] == first_id and custom["fallbackModelId"] == second_id

    renamed = await p3_client.patch(f"/api/ai/models/{first['id']}", headers=admin, json={"displayName": "Renamed", "config": {"b": 1}})
    assert renamed.status_code == 200 and renamed.json()["displayName"] == "Renamed" and renamed.json()["config"] == {"note": "a", "b": 1}

    swapped = await p3_client.patch(f"/api/ai/models/{second['id']}", headers=admin, json={"isDefault": True})
    assert swapped.status_code == 200, swapped.text
    # the fallback model took the default slot; as a default it can no longer be "callable only as fallback"
    assert (swapped.json()["isDefault"], swapped.json()["isFallback"], swapped.json()["status"]) == (True, False, "active")
    assert (await p3_client.get(f"/api/ai/models/{first['id']}", headers=admin)).json()["isDefault"] is False

    retired = await p3_client.patch(f"/api/ai/models/{second['id']}", headers=admin, json={"status": "retired"})
    assert retired.status_code == 200 and (retired.json()["status"], retired.json()["isDefault"]) == ("retired", False)

    deleted = await p3_client.delete(f"/api/ai/models/{first['id']}", headers=admin)
    assert deleted.status_code == 200 and deleted.json() == {"ok": True, "id": first["id"]}
    assert (await p3_client.get(f"/api/ai/models/{first['id']}", headers=admin)).status_code == 404
    assert (await p3_client.delete(f"/api/ai/models/{first['id']}", headers=admin)).status_code == 404

    trail = await _audit(p3_pool, admin_id)
    assert [r["action"] for r in trail] == ["model.create", "model.create", "model.update", "model.update", "model.update", "model.delete"]
    update = trail[2]
    assert json.loads(update["before"])["displayName"] == "First" and json.loads(update["after"])["displayName"] == "Renamed"
    assert trail[0]["resource"] == f"ai_model:{first['id']}" and json.loads(trail[0]["after"])["modelId"] == first_id
    assert json.loads(trail[5]["before"])["modelId"] == first_id and trail[5]["after"] is None


@pytest.mark.asyncio
async def test_models_validation_and_conflict_responses(p3_client, p3_accounts, p3_admin_state):
    _, admin = await p3_accounts(p3_client, role="admin")
    model_id = _mid()
    body = {"providerId": "custom", "modelId": model_id, "displayName": "X"}
    created = (await p3_client.post("/api/ai/models", headers=admin, json=body)).json()

    duplicate = await p3_client.post("/api/ai/models", headers=admin, json=body)
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "conflict"
    unknown_provider = await p3_client.post("/api/ai/models", headers=admin, json={**body, "providerId": "openai"})
    assert unknown_provider.status_code == 400 and "Unknown provider" in unknown_provider.json()["error"]
    contradiction = await p3_client.post("/api/ai/models", headers=admin, json={**body, "modelId": _mid(), "status": "retired", "isDefault": True})
    assert contradiction.status_code == 400 and "cannot be the default" in contradiction.json()["error"]
    assert (await p3_client.post("/api/ai/models", headers=admin, json={**body, "modelId": _mid(), "status": "bogus"})).status_code in (400, 422)

    url = f"/api/ai/models/{created['id']}"
    assert (await p3_client.patch(url, headers=admin, json={})).status_code == 400
    assert (await p3_client.patch(url, headers=admin, json={"status": None})).status_code == 400
    both = await p3_client.patch(url, headers=admin, json={"isDefault": True, "isFallback": True})
    assert both.status_code == 400 and "both" in both.json()["error"]
    assert (await p3_client.patch(url, headers=admin, json={"unknownField": 1})).status_code in (200, 400, 422)
    assert (await p3_client.get("/api/ai/models/not-a-uuid", headers=admin)).status_code == 404
    assert (await p3_client.patch(f"/api/ai/models/{uuid.uuid4()}", headers=admin, json={"displayName": "x"})).status_code == 404
    assert (await p3_client.get("/api/ai/models", params={"provider": "nope"}, headers=admin)).status_code == 400


@pytest.mark.asyncio
async def test_a_model_with_history_is_refused_deletion_with_the_references_listed(p3_client, p3_accounts, p3_admin_state, p3_pool):
    _, admin = await p3_accounts(p3_client, role="admin")
    created = (await p3_client.post("/api/ai/models", headers=admin, json={"providerId": "custom", "modelId": _mid(), "displayName": "H"})).json()
    provider_id = await p3_pool.fetchval("SELECT provider_id FROM ai_models WHERE id = $1", uuid.UUID(created["id"]))
    await p3_pool.execute("INSERT INTO ai_health_checks (provider_id, model_id, status) VALUES ($1, $2, 'ok')", provider_id, uuid.UUID(created["id"]))
    try:
        refused = await p3_client.delete(f"/api/ai/models/{created['id']}", headers=admin)
        assert refused.status_code == 409
        assert refused.json()["details"] == {"references": {"ai_health_checks": 1}} and "retired" in refused.json()["error"]
        assert (await p3_client.get(f"/api/ai/models/{created['id']}", headers=admin)).status_code == 200
    finally:
        await p3_pool.execute("DELETE FROM ai_health_checks WHERE model_id = $1", uuid.UUID(created["id"]))


@pytest.mark.asyncio
async def test_catalogue_sync_endpoint_restores_only_when_forced_and_is_audited(p3_client, p3_accounts, p3_admin_state, p3_pool):
    admin_id, admin = await p3_accounts(p3_client, role="admin")
    await p3_pool.execute("DELETE FROM ai_models WHERE model_id = 'grok-3'")
    plain = await p3_client.post("/api/ai/models/sync-catalogue", headers=admin)
    assert plain.status_code == 200 and plain.json() == {"offered": [], "force": False}
    forced = await p3_client.post("/api/ai/models/sync-catalogue", params={"force": "true"}, headers=admin)
    assert "grok:grok-3" in forced.json()["offered"] and forced.json()["force"] is True
    assert [json.loads(r["after"])["force"] for r in await _audit(p3_pool, admin_id, "model.sync_catalogue")] == [False, True]


# --------------------------------------------------------------------------
# Provider settings (encrypted API keys)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_api_key_is_write_only_encrypted_audited_without_the_secret_and_takes_effect(
    p3_client, p3_accounts, p3_admin_state, p3_pool, monkeypatch
):
    from app.providers.grok_provider import GrokProvider
    from app.security import secret_store as ss

    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", ss.generate_key())
    admin_id, admin = await p3_accounts(p3_client, role="admin")
    secret = "sk-this-must-never-leak-0123456789"
    assert GrokProvider().is_configured() is False

    saved = await p3_client.patch("/api/ai/providers/grok/settings", headers=admin, json={"apiKey": secret, "timeoutMs": 4500, "retryCount": 1, "region": " us-east-1 "})
    assert saved.status_code == 200, saved.text
    view = saved.json()
    assert view["apiKey"] == {"stored": True, "source": "admin_override", "error": None}
    assert (view["timeoutMs"], view["retryCount"], view["region"]) == (
        {"override": 4500, "effective": 4500}, {"override": 1, "effective": 1}, "us-east-1")
    assert view["secretStoreConfigured"] is True and view["updatedBy"] == admin_id
    assert secret not in saved.text and secret not in (await p3_client.get("/api/ai/providers/grok/settings", headers=admin)).text

    stored = await p3_pool.fetchval("SELECT api_key_encrypted FROM ai_provider_settings s JOIN ai_providers p ON p.id = s.provider_id WHERE p.name = 'grok'")
    assert secret not in stored and ss.decrypt_secret(stored) == secret
    assert GrokProvider().is_configured() is True  # no XAI_API_KEY in the environment: the override is what configures it

    audit_text = json.dumps([[dict(r) for r in await _audit(p3_pool, admin_id)]], default=str)
    assert secret not in audit_text and stored not in audit_text
    update = (await _audit(p3_pool, admin_id, "provider.settings_update"))[0]
    assert json.loads(update["after"])["apiKeySet"] is True and json.loads(update["after"])["apiKeyChanged"] is True
    assert json.loads(update["before"]) == {}

    cleared = await p3_client.patch("/api/ai/providers/grok/settings", headers=admin, json={"apiKey": None})
    assert cleared.json()["apiKey"] == {"stored": False, "source": "none", "error": None}
    assert GrokProvider().is_configured() is False
    assert (await p3_client.patch("/api/ai/providers/grok/settings", headers=admin, json={"timeoutMs": None})).json()["timeoutMs"]["override"] is None


@pytest.mark.asyncio
async def test_saving_an_api_key_without_a_master_key_is_refused_and_stores_nothing(p3_client, p3_accounts, p3_admin_state, p3_pool):
    _, admin = await p3_accounts(p3_client, role="admin")
    refused = await p3_client.patch("/api/ai/providers/grok/settings", headers=admin, json={"apiKey": "sk-0123456789abcdef"})
    assert refused.status_code == 503 and refused.json()["code"] == "not_configured"
    assert "SETTINGS_ENCRYPTION_KEY" in refused.json()["error"]
    assert await p3_pool.fetchval("SELECT count(*) FROM ai_provider_settings s JOIN ai_providers p ON p.id = s.provider_id WHERE p.name = 'grok' AND s.api_key_encrypted IS NOT NULL") == 0


@pytest.mark.asyncio
async def test_the_environment_key_is_reported_as_the_source_until_an_override_exists(p3_client, p3_accounts, p3_admin_state, monkeypatch):
    _, admin = await p3_accounts(p3_client, role="admin")
    monkeypatch.setenv("XAI_API_KEY", "env-key-0123456789")
    view = (await p3_client.get("/api/ai/providers/grok/settings", headers=admin)).json()
    assert view["apiKey"]["source"] == "environment" and view["apiKey"]["stored"] is False
    assert view["baseUrl"] == {"override": None, "effective": "https://api.x.ai/v1", "source": "default"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,payload,fragment",
    [
        ("grok", {"baseUrl": "http://api.example.com/v1"}, "https"),
        ("grok", {"baseUrl": "https://user:pw@api.example.com/v1"}, "credentials"),
        ("custom", {"baseUrl": "https://x.example.com/v1?key=1"}, "query"),
        ("custom", {"baseUrl": "ftp://x.example.com"}, "absolute http"),
        ("custom", {"baseUrl": "not a url"}, "absolute http"),
        ("custom", {"apiKey": "short"}, ""),
        ("mock", {"timeoutMs": 2000}, "no settings"),
        ("grok", {}, "No fields"),
    ],
)
async def test_settings_validation(p3_client, p3_accounts, p3_admin_state, monkeypatch, provider, payload, fragment):
    from app.security import secret_store as ss

    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", ss.generate_key())
    _, admin = await p3_accounts(p3_client, role="admin")
    response = await p3_client.patch(f"/api/ai/providers/{provider}/settings", headers=admin, json=payload)
    assert response.status_code in (400, 422), response.text
    if fragment:
        assert fragment in response.text


@pytest.mark.asyncio
async def test_settings_range_and_provider_errors(p3_client, p3_accounts, p3_admin_state):
    _, admin = await p3_accounts(p3_client, role="admin")
    for payload in ({"timeoutMs": 50}, {"timeoutMs": 400000}, {"retryCount": 11}, {"retryCount": -1}, {"unknown": 1}):
        assert (await p3_client.patch("/api/ai/providers/grok/settings", headers=admin, json=payload)).status_code in (400, 422)
    assert (await p3_client.get("/api/ai/providers/nope/settings", headers=admin)).status_code == 404
    assert (await p3_client.patch("/api/ai/providers/nope", headers=admin, json={"enabled": True})).status_code == 404


@pytest.mark.asyncio
async def test_a_custom_base_url_override_is_what_the_provider_calls(p3_client, p3_accounts, p3_admin_state, monkeypatch):
    from app.providers.custom_ai_provider import CustomAIProvider

    _, admin = await p3_accounts(p3_client, role="admin")
    monkeypatch.setenv("CUSTOM_AI_MODEL_ID", "m")
    assert CustomAIProvider().is_configured() is False
    ok = await p3_client.patch("/api/ai/providers/custom/settings", headers=admin, json={"baseUrl": "http://127.0.0.1:8080/v1/"})
    assert ok.status_code == 200 and ok.json()["baseUrl"]["override"] == "http://127.0.0.1:8080/v1"
    # a local runtime needs no API key, so a base URL (+ the model id) is all it takes
    assert CustomAIProvider().base_url() == "http://127.0.0.1:8080/v1" and CustomAIProvider().is_configured() is True
    remote = await p3_client.patch("/api/ai/providers/custom/settings", headers=admin, json={"baseUrl": "https://api.remote.example/v1"})
    assert remote.status_code == 200 and CustomAIProvider().is_configured() is False  # a remote endpoint still needs a key


# --------------------------------------------------------------------------
# Provider enable/disable
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_disabled_provider_is_unavailable_everywhere_and_not_a_breaker_failure(
    p3_client, p3_accounts, p3_admin_state, p3_pool, monkeypatch
):
    from app.providers.circuit_breaker import get_circuit_breakers

    admin_id, admin = await p3_accounts(p3_client, role="admin")
    monkeypatch.setenv("XAI_API_KEY", "env-key-0123456789")
    symbol = "ZZP3DIS"
    try:
        patched = await p3_client.patch("/api/ai/providers/grok", headers=admin, json={"enabled": False, "priority": 3})
        assert patched.status_code == 200 and patched.json() == {"providerId": "grok", "enabled": False, "priority": 3}
        grok = next(p for p in (await p3_client.get("/api/ai/providers")).json() if p["id"] == "grok")
        assert (grok["enabled"], grok["priority"]) == (False, 3)

        route = (await p3_client.post("/api/ai/route", headers=admin, json={"provider": "grok", "allowFallback": True})).json()
        decision = route["decisions"][0]
        assert decision["available"] is True and decision["fellBack"] is True and decision["resolvedProviderId"] == "mock"
        assert "disabled by an administrator" in decision["reason"]

        single = (await p3_client.post("/api/ai/analyze", headers=admin, json={"symbol": symbol, "provider": "grok"})).json()
        assert (single["provider"], single["source"], single["requestedProvider"]) == ("mock", "mock", "grok")
        assert any("disabled by an administrator" in w for w in single["validationWarnings"])

        multi = (await p3_client.post("/api/ai/compare", headers=admin, json={"symbol": symbol, "providers": ["mock", "grok"]})).json()
        slots = {r["providerId"]: r for r in multi["results"]}
        assert slots["mock"]["outcome"] == "ok" and slots["grok"]["outcome"] == "unavailable" and slots["grok"]["reasonCode"] == "disabled"
        assert (await get_circuit_breakers().snapshot("grok")).total_failures == 0

        await p3_client.patch("/api/ai/providers/grok", headers=admin, json={"enabled": True})
        assert next(p for p in (await p3_client.get("/api/ai/providers")).json() if p["id"] == "grok")["enabled"] is True
        actions = [(r["action"], json.loads(r["before"])["enabled"], json.loads(r["after"])["enabled"]) for r in await _audit(p3_pool, admin_id, "provider.update")]
        assert actions == [("provider.update", True, False), ("provider.update", False, True)]
        assert (await p3_client.patch("/api/ai/providers/grok", headers=admin, json={})).status_code == 400
        assert (await p3_client.patch("/api/ai/providers/mock", headers=admin, json={"enabled": False})).status_code == 400
    finally:
        await _drop_instrument(p3_pool, symbol)


# --------------------------------------------------------------------------
# Single-mode substitution signal (defect 21)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_mode_says_so_when_another_provider_answered(p3_client, p3_accounts, p3_pool, monkeypatch):
    from app.providers.grok_provider import GrokProvider

    _, admin = await p3_accounts(p3_client, role="admin")
    symbol = "ZZP3SUB"
    try:
        unconfigured = (await p3_client.post("/api/ai/analyze", headers=admin, json={"symbol": symbol, "provider": "grok"})).json()
        assert (unconfigured["provider"], unconfigured["requestedProvider"]) == ("mock", "grok")
        assert any("Requested provider 'grok' was not used" in w and "XAI_API_KEY" in w for w in unconfigured["validationWarnings"])

        explicit_mock = (await p3_client.post("/api/ai/analyze", headers=admin, json={"symbol": symbol, "provider": "mock"})).json()
        default_mode = (await p3_client.post("/api/ai/analyze", headers=admin, json={"symbol": symbol})).json()
        for ok in (explicit_mock, default_mode):
            assert ok["requestedProvider"] is None
            assert not any("was not used" in w for w in ok["validationWarnings"])

        monkeypatch.setenv("XAI_API_KEY", "env-key-0123456789")

        async def failing(self, context, model_id=None):  # noqa: ANN001
            raise RuntimeError("upstream exploded")

        monkeypatch.setattr(GrokProvider, "reason", failing)
        failed = (await p3_client.post("/api/ai/analyze", headers=admin, json={"symbol": symbol, "provider": "grok"})).json()
        assert (failed["provider"], failed["source"], failed["requestedProvider"]) == ("mock", "fallback", "grok")
        assert any("upstream exploded" in w for w in failed["validationWarnings"])
    finally:
        await _drop_instrument(p3_pool, symbol)


@pytest.mark.asyncio
async def test_a_fallback_model_answer_is_reported_in_the_warnings(p3_client, p3_accounts, p3_admin_state, p3_pool, monkeypatch):
    from app.providers.grok_provider import GrokProvider
    from app.providers.model_health import get_model_health
    from app.providers.runtime_config import reload_models

    _, admin = await p3_accounts(p3_client, role="admin")
    monkeypatch.setenv("XAI_API_KEY", "env-key-0123456789")
    await reload_models()
    seen: list = []

    async def reason(self, context, model_id=None):  # noqa: ANN001
        from app.providers.base import ProviderReasoning

        seen.append(model_id)
        return ProviderReasoning(direction=None, confidence=None, reasoning_summary="no setup", supporting_evidence=[])

    monkeypatch.setattr(GrokProvider, "reason", reason)
    symbol = "ZZP3FBM"
    try:
        get_model_health().record_probe("grok", "grok-4.6", False, "HTTP 500")  # as a failed admin probe would
        body = (await p3_client.post("/api/ai/analyze", headers=admin, json={"symbol": symbol, "provider": "grok"})).json()
        assert seen and seen[0] not in (None, "grok-4.6")
        assert body["provider"] == "grok" and body["requestedProvider"] is None and body["model"] == seen[0]
        assert any("fallback model" in w for w in body["validationWarnings"])
    finally:
        await _drop_instrument(p3_pool, symbol)


@pytest.mark.asyncio
async def test_a_failed_admin_probe_feeds_the_routers_health_stage(p3_client, p3_accounts, p3_admin_state, monkeypatch):
    from app.providers.base import HealthResult
    from app.providers.grok_provider import GrokProvider
    from app.providers.registry import select_call_model
    from app.providers.runtime_config import reload_models

    _, admin = await p3_accounts(p3_client, role="admin")
    monkeypatch.setenv("XAI_API_KEY", "env-key-0123456789")
    await reload_models()
    result = {"ok": False}

    async def health_check(self):  # noqa: ANN001
        return HealthResult(ok=result["ok"], latency_ms=5.0, model_checked="grok-4.6", detail="probe", transport="hosted_api")

    monkeypatch.setattr(GrokProvider, "health_check", health_check)
    assert select_call_model("grok").substituted is False
    probe = await p3_client.post("/api/ai/grok/test", headers=admin)
    assert probe.status_code == 200 and probe.json()["ok"] is False
    assert select_call_model("grok").substituted is True  # the bad probe moved traffic to the fallback model
    result["ok"] = True
    await p3_client.get("/api/ai/grok/health", headers=admin)
    assert select_call_model("grok").substituted is False  # a good probe restores it


@pytest.mark.asyncio
async def test_an_unconfigured_probe_does_not_poison_model_health(p3_client, p3_accounts, p3_admin_state):
    from app.providers.registry import select_call_model

    _, admin = await p3_accounts(p3_client, role="admin")
    probe = await p3_client.post("/api/ai/grok/test", headers=admin)
    assert probe.json()["ok"] is False
    assert select_call_model("grok").substituted is False


@pytest.mark.asyncio
async def test_probe_responses_carry_both_field_spellings(p3_client, p3_accounts):
    _, admin = await p3_accounts(p3_client, role="admin")
    body = (await p3_client.post("/api/ai/mock/test", headers=admin)).json()
    assert body["ok"] is True and body["latency_ms"] == body["latencyMs"] and body["model_checked"] == body["modelChecked"] == "mock-v1"
    health = (await p3_client.get("/api/ai/mock/health", headers=admin)).json()
    assert health["model_checked"] == "mock-v1"


# --------------------------------------------------------------------------
# POST /api/ai/compare
# --------------------------------------------------------------------------


@pytest.fixture
def custom_online(monkeypatch):
    from app.providers.base import ProviderReasoning
    from app.providers.custom_ai_provider import CustomAIProvider

    monkeypatch.setenv("CUSTOM_AI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("CUSTOM_AI_MODEL_ID", "zzp3-custom-model")

    async def reason(self, context, model_id=None):  # noqa: ANN001
        return ProviderReasoning(direction=None, confidence=None, reasoning_summary="declined", supporting_evidence=[])

    monkeypatch.setattr(CustomAIProvider, "reason", reason)


@pytest.mark.asyncio
async def test_compare_is_the_same_path_as_multi_mode_analyze(p3_client, p3_accounts, p3_pool, custom_online):
    user_id, headers = await p3_accounts(p3_client)
    symbol = "ZZP3CMP"
    try:
        compared = await p3_client.post("/api/ai/compare", headers=headers, json={"symbol": symbol, "providers": ["mock", "custom", "grok"]})
        analyzed = await p3_client.post("/api/ai/analyze", headers=headers, json={"symbol": symbol, "mode": "multi", "providers": ["mock", "custom", "grok"]})
        assert compared.status_code == analyzed.status_code == 200, (compared.text, analyzed.text)
        a, b = compared.json(), analyzed.json()
        assert a.keys() == b.keys()
        assert [(r["providerId"], r["outcome"]) for r in a["results"]] == [(r["providerId"], r["outcome"]) for r in b["results"]]
        assert {r["providerId"]: r["outcome"] for r in a["results"]} == {"mock": "ok", "custom": "ok", "grok": "unavailable"}
        assert a["comparisonId"] and b["comparisonId"] and a["comparisonId"] != b["comparisonId"]
        assert await p3_pool.fetchval("SELECT count(*) FROM ai_comparisons c JOIN conversations v ON v.id = c.conversation_id WHERE v.user_id = $1", user_id) == 2
        assert await p3_pool.fetchval(
            "SELECT count(*) FROM ai_comparison_results r JOIN ai_comparisons c ON c.id = r.comparison_id JOIN conversations v ON v.id = c.conversation_id WHERE v.user_id = $1", user_id) == 6
    finally:
        await _drop_instrument(p3_pool, symbol)
        await p3_pool.execute("DELETE FROM ai_models WHERE model_id = 'zzp3-custom-model'")


@pytest.mark.asyncio
async def test_compare_accepts_the_specified_provider_id_spelling_and_validates_the_count(p3_client, p3_accounts, p3_pool, custom_online):
    _, headers = await p3_accounts(p3_client)
    symbol = "ZZP3CMP2"
    try:
        spelled = await p3_client.post("/api/ai/compare", headers=headers, json={"symbol": symbol, "provider_ids": ["mock", "custom"]})
        assert spelled.status_code == 200 and [r["providerId"] for r in spelled.json()["results"]] == ["mock", "custom"]
        for bad in (["mock"], ["mock", "custom", "grok", "gemma"], [], ["mock", "mock"], ["mock", "unknown"]):
            response = await p3_client.post("/api/ai/compare", headers=headers, json={"symbol": symbol, "providers": bad})
            assert response.status_code in (400, 422), (bad, response.text)
        assert (await p3_client.post("/api/ai/compare", json={"symbol": symbol, "providers": ["mock", "custom"]})).status_code == 401
    finally:
        await _drop_instrument(p3_pool, symbol)
        await p3_pool.execute("DELETE FROM ai_models WHERE model_id = 'zzp3-custom-model'")


def test_compare_shares_the_analyze_rate_limit_bucket_and_admin_paths_share_the_admin_bucket():
    from app.config import get_settings
    from app.rate_limit import _limit_for_path

    settings = get_settings().rate_limit
    assert _limit_for_path("/api/ai/compare", settings)[0] == _limit_for_path("/api/ai/analyze", settings)[0] == "analyze"
    admin = _limit_for_path("/api/ai/models", settings)[0]
    for path in ("/api/ai/circuit-breaker/settings", "/api/ai/providers/grok/settings", "/api/ai/providers/grok", "/api/ai/grok/test"):
        assert _limit_for_path(path, settings)[0] == admin, path
    assert _limit_for_path("/api/ai/providers", settings)[0] != admin  # the public provider list keeps the default bucket


# --------------------------------------------------------------------------
# Circuit-breaker administration (G03)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breaker_thresholds_are_admin_configurable_persisted_live_and_audited(p3_client, p3_accounts, p3_admin_state, p3_pool):
    from app.providers.circuit_breaker import get_circuit_breakers

    admin_id, admin = await p3_accounts(p3_client, role="admin")
    initial = (await p3_client.get("/api/ai/circuit-breaker", headers=admin)).json()
    assert initial["source"] == "environment" and initial["defaults"]["failureThreshold"] == 5
    assert [b["providerId"] for b in initial["breakers"]] == ["mock", "custom", "grok", "gemma"]

    put = await p3_client.put("/api/ai/circuit-breaker/settings", headers=admin, json={"failureThreshold": 2, "providers": {"grok": {"cooldownSeconds": 5}}})
    assert put.status_code == 200, put.text
    view = put.json()
    assert view["source"] == "database" and view["defaults"]["failureThreshold"] == 2
    assert view["providers"]["grok"] == {"failureThreshold": 2, "windowSeconds": 60.0, "cooldownSeconds": 5.0}
    grok = next(b for b in view["breakers"] if b["providerId"] == "grok")
    assert (grok["failureThreshold"], grok["cooldownSeconds"]) == (2, 5.0)
    assert (await get_circuit_breakers().snapshot("gemma")).failure_threshold == 2  # live, no restart

    assert json.loads(await p3_pool.fetchval("SELECT value FROM system_settings WHERE key = 'circuit_breaker'")) == {
        "failureThreshold": 2, "providers": {"grok": {"cooldownSeconds": 5}}}
    reset_to_env = await p3_client.delete("/api/ai/circuit-breaker/settings", headers=admin)
    assert reset_to_env.json()["source"] == "environment" and reset_to_env.json()["defaults"]["failureThreshold"] == 5
    assert await p3_pool.fetchval("SELECT count(*) FROM system_settings WHERE key = 'circuit_breaker'") == 0

    trail = await _audit(p3_pool, admin_id)
    assert [r["action"] for r in trail] == ["circuit_breaker.settings_update", "circuit_breaker.settings_reset"]
    assert json.loads(trail[0]["after"])["failureThreshold"] == 2 and trail[0]["before"] is None
    assert json.loads(trail[1]["before"])["failureThreshold"] == 2


@pytest.mark.asyncio
async def test_breaker_settings_validation(p3_client, p3_accounts, p3_admin_state):
    _, admin = await p3_accounts(p3_client, role="admin")
    for payload in ({"failureThreshold": 0}, {"failureThreshold": 101}, {"windowSeconds": 0.5}, {"cooldownSeconds": 4000},
                    {"unknown": 1}, {"providers": {"grok": {"failureThreshold": 0}}}):
        assert (await p3_client.put("/api/ai/circuit-breaker/settings", headers=admin, json=payload)).status_code in (400, 422), payload
    unknown = await p3_client.put("/api/ai/circuit-breaker/settings", headers=admin, json={"providers": {"openai": {"failureThreshold": 3}}})
    assert unknown.status_code == 400 and "openai" in unknown.json()["error"]
    assert (await p3_client.put("/api/ai/circuit-breaker/settings", headers=admin, json={})).json()["source"] == "database"  # {} = "defaults", stored explicitly


@pytest.mark.asyncio
async def test_an_admin_can_reset_a_tripped_breaker(p3_client, p3_accounts, p3_pool):
    from app.providers.circuit_breaker import get_circuit_breakers

    admin_id, admin = await p3_accounts(p3_client, role="admin")
    breakers = get_circuit_breakers()
    for _ in range(5):
        await breakers.record_failure("gemma", "down")
    tripped = next(b for b in (await p3_client.get("/api/ai/circuit-breaker", headers=admin)).json()["breakers"] if b["providerId"] == "gemma")
    assert tripped["state"] == "open"
    reset = await p3_client.post("/api/ai/circuit-breaker/gemma/reset", headers=admin)
    assert reset.status_code == 200 and reset.json() == {"providerId": "gemma", "state": "closed"}
    assert (await breakers.snapshot("gemma")).state == "closed"
    audit = (await _audit(p3_pool, admin_id, "circuit_breaker.reset"))[0]
    assert json.loads(audit["before"])["state"] == "open" and json.loads(audit["after"]) == {"state": "closed"}
    assert (await p3_client.post("/api/ai/circuit-breaker/nope/reset", headers=admin)).status_code == 404


# --------------------------------------------------------------------------
# Availability poll (mock-tested: no vendor list endpoint is reachable from here)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_availability_poll_retires_a_missing_default_persists_alerts_and_audits(
    p3_client, p3_accounts, p3_admin_state, p3_pool, monkeypatch
):
    from app.providers.grok_provider import GrokProvider
    from app.providers.model_registry import configured_model_id
    from app.providers.runtime_config import reload_models

    admin_id, admin = await p3_accounts(p3_client, role="admin")
    monkeypatch.setenv("XAI_API_KEY", "env-key-0123456789")
    await reload_models()
    fallback = (await p3_client.get("/api/ai/providers")).json()
    fallback_id = next(p for p in fallback if p["id"] == "grok")["fallbackModelId"]
    assert fallback_id

    async def listed(self):  # noqa: ANN001
        return {fallback_id, "some-other-model"}  # grok-4.6 (the default) is no longer offered

    monkeypatch.setattr(GrokProvider, "list_available_models", listed)
    response = await p3_client.post("/api/ai/models/availability-check", headers=admin)
    assert response.status_code == 200, response.text
    results = {r["providerId"]: r for r in response.json()["results"]}
    assert results["grok"]["checked"] is True and results["grok"]["retired"] == ["grok-4.6"] and results["grok"]["modelsOffered"] == 2
    assert results["gemma"]["checked"] is False and "not configured" in results["gemma"]["skippedReason"]

    assert await p3_pool.fetchval("SELECT m.status FROM ai_models m JOIN ai_providers p ON p.id = m.provider_id WHERE p.name='grok' AND m.model_id='grok-4.6'") == "retired"
    assert configured_model_id("grok") == fallback_id
    alert = await p3_pool.fetchrow("SELECT severity, message FROM error_logs WHERE type = 'ModelRetired' ORDER BY ts DESC LIMIT 1")
    assert alert["severity"] == "high" and "grok-4.6" in alert["message"]
    actions = [r["action"] for r in await _audit(p3_pool, admin_id)]
    assert actions == ["model.availability_check"]  # the auto-retire audit row has a null actor
    assert json.loads((await _audit(p3_pool, admin_id))[0]["after"]) == {"retired": ["grok:grok-4.6"]}
    assert await p3_pool.fetchval("SELECT count(*) FROM audit_logs WHERE action = 'model.auto_retire' AND actor_id IS NULL AND ts > now() - interval '1 minute'") >= 1

    again = (await p3_client.post("/api/ai/models/availability-check", headers=admin)).json()
    assert next(r for r in again["results"] if r["providerId"] == "grok")["retired"] == []  # idempotent


@pytest.mark.asyncio
async def test_a_vendor_404_during_a_real_call_retires_the_model_persistently(p3_client, p3_accounts, p3_admin_state, p3_pool, monkeypatch):
    from app.providers import grok_provider as gp
    from app.providers.transport import ProviderTransportError
    from app.providers.runtime_config import reload_models

    _, admin = await p3_accounts(p3_client, role="admin")
    monkeypatch.setenv("XAI_API_KEY", "env-key-0123456789")
    await reload_models()

    async def vendor_404(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ProviderTransportError("Grok API error 404: The model grok-4.6 does not exist", status_code=404)

    monkeypatch.setattr(gp, "post_json", vendor_404)
    symbol = "ZZP3RET"
    try:
        body = (await p3_client.post("/api/ai/analyze", headers=admin, json={"symbol": symbol, "provider": "grok"})).json()
        assert (body["provider"], body["source"], body["requestedProvider"]) == ("mock", "fallback", "grok")  # the request still succeeded
        assert await p3_pool.fetchval("SELECT m.status FROM ai_models m JOIN ai_providers p ON p.id = m.provider_id WHERE p.name='grok' AND m.model_id='grok-4.6'") == "retired"
        assert await p3_pool.fetchval("SELECT count(*) FROM error_logs WHERE type = 'ModelRetired'") == 1
    finally:
        await _drop_instrument(p3_pool, symbol)


# --------------------------------------------------------------------------
# What the open provider list reveals
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_public_provider_list_exposes_state_but_never_a_credential(p3_client, p3_accounts, p3_admin_state, monkeypatch):
    from app.security import secret_store as ss

    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", ss.generate_key())
    _, admin = await p3_accounts(p3_client, role="admin")
    secret = "sk-public-list-must-not-show-this-1234"
    await p3_client.patch("/api/ai/providers/grok/settings", headers=admin, json={"apiKey": secret, "baseUrl": "https://proxy.example.com/v1"})
    anonymous = await p3_client.get("/api/ai/providers")
    assert anonymous.status_code == 200 and secret not in anonymous.text and "proxy.example.com" not in anonymous.text
    grok = next(p for p in anonymous.json() if p["id"] == "grok")
    assert {"enabled", "priority", "modelSelectionSource", "fallbackModelId", "defaultModelId"} <= grok.keys()
    assert grok["modelSelectionSource"] == "registry" and grok["configured"] is True
    monkeypatch.setenv("GROK_MODEL_ID", "grok-4.3")
    assert next(p for p in (await p3_client.get("/api/ai/providers")).json() if p["id"] == "grok")["modelSelectionSource"] == "environment"
