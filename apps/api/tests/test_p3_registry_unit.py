"""
Phase P3 — the model registry, router health stage, provider settings, secret store,
circuit-breaker configuration and model lifecycle. No database required (the DB-backed
half is tests/test_p3_persistence.py).
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import CircuitBreakerSettings
from app.db.models import ModelRow, ModelValidationError, validate_state
from app.providers import provider_settings as ps
from app.providers.base import (
    AnalysisContext,
    ProviderDisabledError,
    ProviderNoModelError,
    ProviderReasoning,
)
from app.providers.model_health import get_model_health, reset_model_health_for_tests
from app.providers.model_registry import (
    configured_model_id,
    get_model_registry,
    model_selection_source,
    reset_model_registry_for_tests,
)

ENV_NAMES = (
    "XAI_API_KEY", "GOOGLE_AI_API_KEY", "GEMMA_TRANSPORT", "GEMMA_LOCAL_BASE_URL",
    "GROK_MODEL_ID", "GEMMA_MODEL_ID", "CUSTOM_AI_BASE_URL", "CUSTOM_AI_MODEL_ID",
    "CUSTOM_AI_API_KEY", "SETTINGS_ENCRYPTION_KEY", "DATABASE_URL",
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    reset_model_registry_for_tests()
    reset_model_health_for_tests()
    ps.reset_for_tests()
    yield
    reset_model_registry_for_tests()
    reset_model_health_for_tests()
    ps.reset_for_tests()


def _row(provider, model_id, status="active", *, default=False, fallback=False, config=None, name=None) -> ModelRow:
    return ModelRow(
        id=f"id-{provider}-{model_id}", provider=provider, provider_db_id=f"pid-{provider}",
        model_id=model_id, display_name=name or model_id, status=status, context_window=None,
        input_modalities=["text"], output_modalities=["text"], is_default=default, is_fallback=fallback,
        online_capable=True, offline_capable=False, config=config or {},
    )


def _context() -> AnalysisContext:
    from app.indicators import compute_indicators, generate_synthetic_ohlcv

    bars = generate_synthetic_ohlcv("P3UNIT", 60)
    return AnalysisContext(symbol="P3UNIT", indicators=compute_indicators(bars),
                           last_bars=[{"timestamp": b.timestamp, "close": b.close} for b in bars[-5:]])


def _reasoning() -> ProviderReasoning:
    return ProviderReasoning(direction="LONG", confidence=60, reasoning_summary="ok", supporting_evidence=["rsi14"])


# --------------------------------------------------------------------------
# Registry invariants
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,default,fallback,ok",
    [
        ("active", True, False, True), ("hosted", True, False, True), ("local", True, False, True),
        ("fallback", True, False, False), ("deprecated", True, False, False),
        ("disabled", True, False, False), ("retired", True, False, False),
        ("fallback", False, True, True), ("active", False, True, True),
        ("deprecated", False, True, False), ("disabled", False, True, False), ("retired", False, True, False),
        ("active", True, True, False), ("active", False, False, True), ("retired", False, False, True),
    ],
)
def test_registry_state_rules(status, default, fallback, ok):
    if ok:
        validate_state(status, default, fallback)
    else:
        with pytest.raises(ModelValidationError):
            validate_state(status, default, fallback)


def test_an_unknown_status_is_rejected():
    with pytest.raises(ModelValidationError, match="status must be one of"):
        validate_state("experimental", False, False)


def test_registry_and_database_agree_on_which_statuses_are_callable():
    from app.db import models as db_models
    from app.providers import model_registry as reg

    assert db_models.DEFAULT_ELIGIBLE is reg.DEFAULT_ELIGIBLE and db_models.FALLBACK_ELIGIBLE is reg.FALLBACK_ELIGIBLE


def test_catalogue_is_unchanged_until_a_database_is_loaded():
    registry = get_model_registry()
    assert registry.loaded_from_db is False
    assert registry.default_model_id("grok") == "grok-4.6"
    assert registry.fallback_model_id("grok") is not None


def test_registry_instances_never_mutate_the_module_level_seed():
    first = get_model_registry()
    first.mark_model_retired("grok", "grok-4.6", "test")
    reset_model_registry_for_tests()
    assert get_model_registry().get("grok", "grok-4.6").status != "retired"


def test_loaded_rows_replace_the_catalogue_entirely():
    registry = get_model_registry()
    registry.load_rows([_row("grok", "g-new", default=True), _row("grok", "g-old", fallback=True)])
    assert registry.loaded_from_db is True
    assert registry.default_model_id("grok") == "g-new"
    assert registry.fallback_model_id("grok") == "g-old"
    assert registry.get("grok", "grok-4.6") is None
    assert registry.default_model_id("gemma") is None  # not in the loaded rows: no silent catalogue leak


def test_only_callable_statuses_can_be_default_or_fallback_when_selecting():
    registry = get_model_registry()
    registry.load_rows([_row("grok", "g1", "retired", default=True), _row("grok", "g2", "disabled", fallback=True)])
    assert registry.default_model_id("grok") is None and registry.fallback_model_id("grok") is None


def test_row_config_drives_redirects_and_routability():
    registry = get_model_registry()
    registry.load_rows([
        _row("grok", "old", "retired", config={"redirectsTo": "new", "routableForAnalysis": False}),
        _row("grok", "new", default=True),
    ])
    assert registry.resolve_model_slug("grok", "old")[0] == "new"
    assert registry.get("grok", "old").routable_for_analysis is False


def test_retiring_the_default_promotes_the_fallback_in_memory_like_the_database_does():
    registry = get_model_registry()
    registry.load_rows([_row("grok", "g1", default=True), _row("grok", "g2", "fallback", fallback=True)])
    registry.mark_model_retired("grok", "g1", "gone")
    assert registry.get("grok", "g1").status == "retired" and not registry.get("grok", "g1").is_default
    assert registry.default_model_id("grok") == "g2"
    assert registry.get("grok", "g2").status == "active"  # 'fallback' status cannot be a default's
    assert registry.fallback_model_id("grok") is None  # the fallback slot is left empty


# --------------------------------------------------------------------------
# configured_model_id precedence
# --------------------------------------------------------------------------


def test_environment_variable_still_wins_and_is_reported_as_the_source(monkeypatch):
    get_model_registry().load_rows([_row("grok", "from-registry", default=True)])
    assert configured_model_id("grok") == "from-registry" and model_selection_source("grok") == "registry"
    monkeypatch.setenv("GROK_MODEL_ID", "pinned-by-ops")
    assert configured_model_id("grok") == "pinned-by-ops" and model_selection_source("grok") == "environment"


def test_custom_ai_takes_its_model_from_an_admin_created_registry_entry(monkeypatch):
    assert configured_model_id("custom") == "unset"  # the catalogue placeholder is never a model id
    get_model_registry().load_rows([_row("custom", "my-llama", default=True)])
    assert configured_model_id("custom") == "my-llama"
    from app.providers.custom_ai_provider import CustomAIProvider

    monkeypatch.setenv("CUSTOM_AI_BASE_URL", "http://127.0.0.1:9/v1")
    provider = CustomAIProvider()
    assert provider.model_id() == "my-llama" and provider.is_configured() is True
    monkeypatch.setenv("CUSTOM_AI_MODEL_ID", "env-model")
    assert provider.model_id() == "env-model"


# --------------------------------------------------------------------------
# Router: health stage, fallback model, states
# --------------------------------------------------------------------------


def _two_grok_models():
    get_model_registry().load_rows([_row("grok", "g-default", default=True), _row("grok", "g-fallback", fallback=True)])


def test_a_healthy_default_model_is_selected_untouched():
    from app.providers.registry import select_call_model

    _two_grok_models()
    selection = select_call_model("grok")
    assert (selection.model_id, selection.substituted, selection.note) == ("g-default", False, None)


def test_an_unhealthy_default_switches_to_the_providers_fallback_model():
    from app.providers.registry import select_call_model

    _two_grok_models()
    get_model_health().record_probe("grok", "g-default", False, "HTTP 500")
    selection = select_call_model("grok")
    assert selection.model_id == "g-fallback" and selection.substituted is True
    assert "g-default" in selection.note and "HTTP 500" in selection.note


def test_with_no_healthy_fallback_the_default_is_still_attempted():
    from app.providers.registry import select_call_model

    _two_grok_models()
    health = get_model_health()
    health.record_probe("grok", "g-default", False, "down")
    health.record_probe("grok", "g-fallback", False, "down")
    selection = select_call_model("grok")
    assert selection.model_id == "g-default" and selection.substituted is False
    assert "attempting it anyway" in selection.note


def test_model_health_recovers_on_success_and_expires(monkeypatch):
    import app.providers.model_health as mh

    health = get_model_health()
    health.record_failure("grok", "m", "e1")
    assert health.is_healthy("grok", "m") is True  # one failure is not enough
    health.record_failure("grok", "m", "e2")
    assert health.is_healthy("grok", "m") is False
    health.record_success("grok", "m")
    assert health.is_healthy("grok", "m") is True
    health.record_probe("grok", "m", False)
    assert health.is_healthy("grok", "m") is False
    clock = {"t": mh.time.monotonic()}
    monkeypatch.setattr(mh.time, "monotonic", lambda: clock["t"] + mh.TTL_SECONDS + 1)
    assert health.is_healthy("grok", "m") is True  # the flag expires; the next real call is the probe


@pytest.mark.asyncio
async def test_two_failures_move_traffic_to_the_fallback_model_and_report_it(monkeypatch):
    from app.providers.grok_provider import GrokProvider
    from app.providers.registry import execute_provider

    monkeypatch.setenv("XAI_API_KEY", "k" * 12)
    _two_grok_models()
    calls: list[str | None] = []

    async def reason(self, context, model_id=None):  # noqa: ANN001
        calls.append(model_id)
        if model_id is None:
            raise RuntimeError("default model is failing")
        return _reasoning()

    monkeypatch.setattr(GrokProvider, "reason", reason)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="default model is failing"):
            await execute_provider("grok", _context(), allow_fallback=False)
    result = await execute_provider("grok", _context(), allow_fallback=False)
    assert calls == [None, None, "g-fallback"]
    assert result.model_id == "g-fallback" and result.provider_id == "grok" and result.fell_back is False
    assert result.source_tag == "hosted_api"  # a different MODEL of the same provider is not a "fallback" source
    assert "g-fallback" in result.model_note


@pytest.mark.asyncio
async def test_a_disabled_provider_is_a_state_not_a_failure(monkeypatch):
    from app.providers.circuit_breaker import get_circuit_breakers
    from app.providers.registry import execute_provider, provider_unavailability_reason, resolve_provider

    monkeypatch.setenv("XAI_API_KEY", "k" * 12)
    ps.set_provider_flags({"grok": (False, 0)})
    for _ in range(8):
        with pytest.raises(ProviderDisabledError):
            await execute_provider("grok", _context(), allow_fallback=False)
    assert (await get_circuit_breakers().snapshot("grok")).total_failures == 0
    degraded = await execute_provider("grok", _context(), allow_fallback=True)
    assert (degraded.provider_id, degraded.source_tag, degraded.fell_back) == ("mock", "fallback", True)
    assert "disabled by an administrator" in degraded.fallback_reason
    assert resolve_provider("grok").id == "mock"
    assert "disabled" in provider_unavailability_reason("grok")


@pytest.mark.asyncio
async def test_a_provider_whose_models_are_all_retired_has_no_usable_model(monkeypatch):
    from app.providers.registry import execute_provider

    monkeypatch.setenv("XAI_API_KEY", "k" * 12)
    get_model_registry().load_rows([_row("grok", "g1", "retired"), _row("grok", "g2", "disabled")])
    with pytest.raises(ProviderNoModelError):
        await execute_provider("grok", _context(), allow_fallback=False)
    degraded = await execute_provider("grok", _context(), allow_fallback=True)
    assert degraded.fell_back is True and "no usable model" in degraded.fallback_reason


@pytest.mark.asyncio
async def test_unconfigured_reason_is_reported_through_the_substitution_helper():
    from app.providers.registry import provider_unavailability_reason

    assert "XAI_API_KEY" in provider_unavailability_reason("grok")
    assert provider_unavailability_reason("mock") is None
    assert "Unknown provider" in provider_unavailability_reason("nope")
    assert provider_unavailability_reason(None) is None


@pytest.mark.asyncio
async def test_cancelling_a_half_open_trial_does_not_lock_the_provider_out(monkeypatch):
    """Defect 22: a client disconnect mid-trial used to leave `half_open_in_flight` set forever."""
    from app.providers.circuit_breaker import get_circuit_breakers
    from app.providers.grok_provider import GrokProvider
    from app.providers.registry import execute_provider

    monkeypatch.setenv("XAI_API_KEY", "k" * 12)
    get_model_registry().load_rows([_row("grok", "g1", default=True)])
    breakers = get_circuit_breakers()
    breakers.apply_settings(CircuitBreakerSettings(failure_threshold=1, window_seconds=60, cooldown_seconds=0.05))

    async def failing(self, context, model_id=None):  # noqa: ANN001
        raise RuntimeError("down")

    monkeypatch.setattr(GrokProvider, "reason", failing)
    with pytest.raises(RuntimeError):
        await execute_provider("grok", _context(), allow_fallback=False)
    assert (await breakers.snapshot("grok")).state == "open"
    await asyncio.sleep(0.08)  # cooldown elapsed: the next call is the half-open trial

    started = asyncio.Event()

    async def hangs(self, context, model_id=None):  # noqa: ANN001
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(GrokProvider, "reason", hangs)
    task = asyncio.create_task(execute_provider("grok", _context(), allow_fallback=False))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def healthy(self, context, model_id=None):  # noqa: ANN001
        return _reasoning()

    monkeypatch.setattr(GrokProvider, "reason", healthy)
    result = await execute_provider("grok", _context(), allow_fallback=False)  # would raise CircuitBreakerOpen before the fix
    assert result.provider_id == "grok"
    assert (await breakers.snapshot("grok")).state == "closed"


# --------------------------------------------------------------------------
# Circuit-breaker configuration (G03)
# --------------------------------------------------------------------------


def test_breaker_config_merges_over_the_environment_defaults(monkeypatch):
    from app.providers.runtime_config import parse_breaker_config

    monkeypatch.setenv("CB_FAILURE_THRESHOLD", "5")
    from app.config import get_settings

    get_settings(refresh=True)
    default, per_provider = parse_breaker_config(
        {"failureThreshold": 3, "providers": {"grok": {"cooldownSeconds": 10}, "gemma": "garbage"}}
    )
    assert (default.failure_threshold, default.window_seconds) == (3, 60.0)
    assert per_provider["grok"].failure_threshold == 3 and per_provider["grok"].cooldown_seconds == 10.0
    assert "gemma" not in per_provider
    get_settings(refresh=True)


@pytest.mark.parametrize("stored", [None, "nonsense", [], {"failureThreshold": 0}, {"failureThreshold": True},
                                    {"windowSeconds": 99999}, {"cooldownSeconds": -1}])
def test_invalid_stored_breaker_values_fall_back_field_by_field(stored):
    from app.config import get_settings
    from app.providers.runtime_config import parse_breaker_config

    base = get_settings().circuit_breaker
    default, per_provider = parse_breaker_config(stored)
    assert default == base and per_provider == {}


@pytest.mark.asyncio
async def test_new_thresholds_apply_live_and_keep_existing_breaker_state():
    from app.providers.circuit_breaker import CircuitBreakerRegistry

    registry = CircuitBreakerRegistry(CircuitBreakerSettings(failure_threshold=5, window_seconds=60, cooldown_seconds=60))
    for _ in range(3):
        await registry.record_failure("grok", "e")
    assert (await registry.snapshot("grok")).state == "closed"
    registry.apply_settings(
        CircuitBreakerSettings(failure_threshold=5, window_seconds=60, cooldown_seconds=60),
        {"grok": CircuitBreakerSettings(failure_threshold=3, window_seconds=60, cooldown_seconds=30)},
    )
    await registry.record_failure("grok", "e")  # the 4th recorded failure now exceeds grok's new threshold of 3
    snapshot = await registry.snapshot("grok")
    assert snapshot.state == "open" and snapshot.failure_threshold == 3 and snapshot.cooldown_seconds == 30
    assert (await registry.snapshot("gemma")).failure_threshold == 5  # other providers keep the default


# --------------------------------------------------------------------------
# Provider settings resolver
# --------------------------------------------------------------------------


def test_override_beats_environment_beats_default(monkeypatch):
    assert ps.effective_api_key("grok", "XAI_API_KEY") == "" and ps.api_key_source("grok", "XAI_API_KEY") == "none"
    monkeypatch.setenv("XAI_API_KEY", "from-env-key")
    assert ps.effective_api_key("grok", "XAI_API_KEY") == "from-env-key"
    assert ps.api_key_source("grok", "XAI_API_KEY") == "environment"
    ps.set_overrides({"grok": ps.ProviderOverrides(api_key="from-admin-key")})
    assert ps.effective_api_key("grok", "XAI_API_KEY") == "from-admin-key"
    assert ps.api_key_source("grok", "XAI_API_KEY") == "admin_override"
    assert ps.effective_api_key("gemma", "GOOGLE_AI_API_KEY") == ""  # overrides are per provider


def test_base_url_resolution_and_trailing_slash(monkeypatch):
    assert ps.effective_base_url("custom", "CUSTOM_AI_BASE_URL", "") == ""
    monkeypatch.setenv("CUSTOM_AI_BASE_URL", "http://env.example/v1/")
    assert ps.effective_base_url("custom", "CUSTOM_AI_BASE_URL", "") == "http://env.example/v1"
    ps.set_overrides({"custom": ps.ProviderOverrides(base_url="http://admin.example/v1/")})
    assert ps.effective_base_url("custom", "CUSTOM_AI_BASE_URL", "") == "http://admin.example/v1"
    assert ps.base_url_source("custom", "CUSTOM_AI_BASE_URL") == "admin_override"


def test_transport_policy_overrides_are_per_provider():
    base = ps.effective_policy("grok")
    ps.set_overrides({"grok": ps.ProviderOverrides(timeout_ms=1500, retry_count=0)})
    tuned = ps.effective_policy("grok")
    assert tuned.timeout_seconds == 1.5 and tuned.max_attempts == 1
    assert ps.effective_policy("gemma") == base


def test_providers_read_the_resolver_not_the_environment(monkeypatch):
    from app.providers.gemma_provider import GemmaProvider
    from app.providers.grok_provider import GrokProvider

    grok, gemma = GrokProvider(), GemmaProvider()
    assert grok.is_configured() is False and gemma.is_configured() is False
    ps.set_overrides({"grok": ps.ProviderOverrides(api_key="admin-grok-key"), "gemma": ps.ProviderOverrides(api_key="admin-gemma-key")})
    assert grok.is_configured() is True and gemma.is_configured() is True
    monkeypatch.setenv("GEMMA_TRANSPORT", "local")
    ps.set_overrides({"gemma": ps.ProviderOverrides(base_url="http://runtime.lan:8080/v1")})
    assert GemmaProvider().local_base_url() == "http://runtime.lan:8080/v1"


# --------------------------------------------------------------------------
# Secret store
# --------------------------------------------------------------------------


def test_secret_store_round_trip_rotation_and_tamper_detection(monkeypatch):
    from app.security import secret_store as ss

    assert ss.is_configured() is False
    with pytest.raises(ss.SecretStoreError, match="SETTINGS_ENCRYPTION_KEY is not set"):
        ss.encrypt_secret("secret-value")
    old, new = ss.generate_key(), ss.generate_key()
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", old)
    token = ss.encrypt_secret("sk-live-abcdef123456")
    assert "sk-live" not in token and ss.decrypt_secret(token) == "sk-live-abcdef123456"
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", f"{new},{old}")  # rotation: new key first, old still decrypts
    assert ss.decrypt_secret(token) == "sk-live-abcdef123456"
    assert ss.decrypt_secret(ss.encrypt_secret("fresh")) == "fresh"
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", new)  # old key rotated out
    with pytest.raises(ss.SecretStoreError, match="could not be decrypted"):
        ss.decrypt_secret(token)
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", old)
    with pytest.raises(ss.SecretStoreError, match="could not be decrypted"):
        ss.decrypt_secret(token[:-4] + "AAAA")  # tampered ciphertext


def test_secret_store_rejects_malformed_keys_and_empty_secrets(monkeypatch):
    from app.security import secret_store as ss

    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", "not-a-fernet-key")
    assert ss.is_configured() is False
    with pytest.raises(ss.SecretStoreError, match="not a valid"):
        ss.encrypt_secret("x" * 10)
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", ss.generate_key())
    with pytest.raises(ss.SecretStoreError, match="empty"):
        ss.encrypt_secret("")


# --------------------------------------------------------------------------
# Model lifecycle (G02g)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,message,model,expected",
    [
        (404, "Grok API error 404: The model grok-9 does not exist", "grok-9", True),
        (404, "Grok API error 404: Not Found", "grok-9", False),            # wrong URL, not a missing model
        (400, "Grok API error 400: model `grok-9` has been retired", "grok-9", True),
        (400, "Grok API error 400: max_tokens is invalid for this model", "grok-9", False),
        (410, "model no longer available", "grok-9", True),
        (429, "rate limited, model not found in quota table", "grok-9", False),
        (500, "model not found", "grok-9", False),
        (None, "404 NOT_FOUND. models/gemma-x is not found for API version v1beta", "gemma-x", True),
        (None, "model server host not found", "gemma-x", False),            # network error mentioning "model"
        (None, "models/other is not found", "gemma-x", False),              # about a different model
        (404, "", "grok-9", False),
    ],
)
def test_model_not_found_classification_is_conservative(status, message, model, expected):
    from app.providers.lifecycle import is_model_not_found

    assert is_model_not_found(status, message, model) is expected


@pytest.mark.asyncio
async def test_retiring_a_model_without_a_database_flips_memory_and_is_idempotent():
    from app.providers.lifecycle import retire_model

    registry = get_model_registry()
    registry.load_rows([_row("grok", "g1", default=True), _row("grok", "g2", fallback=True)])
    get_model_health().record_failure("grok", "g1", "x")
    first = await retire_model("grok", "g1", "vendor said 404", detected_by="test")
    assert (first.changed, first.was_in_use, first.replacement, first.persisted) == (True, True, "g2", False)
    assert registry.get("grok", "g1").status == "retired"
    assert configured_model_id("grok") == "g2"
    again = await retire_model("grok", "g1", "vendor said 404", detected_by="test")
    assert again.changed is False  # no second flip, no second alert


@pytest.mark.asyncio
async def test_the_grok_provider_retires_a_model_the_vendor_says_is_gone(monkeypatch):
    from app.providers import grok_provider as gp
    from app.providers.grok_provider import GrokProvider
    from app.providers.transport import ProviderTransportError

    monkeypatch.setenv("XAI_API_KEY", "k" * 12)
    registry = get_model_registry()
    registry.load_rows([_row("grok", "g1", default=True), _row("grok", "g2", fallback=True)])

    async def vendor_404(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ProviderTransportError("Grok API error 404: The model g1 does not exist", status_code=404)

    monkeypatch.setattr(gp, "post_json", vendor_404)
    with pytest.raises(ProviderTransportError):
        await GrokProvider().reason(_context())
    assert registry.get("grok", "g1").status == "retired" and configured_model_id("grok") == "g2"


@pytest.mark.asyncio
async def test_a_wrong_base_url_404_does_not_retire_a_model(monkeypatch):
    from app.providers import grok_provider as gp
    from app.providers.grok_provider import GrokProvider
    from app.providers.transport import ProviderTransportError

    monkeypatch.setenv("XAI_API_KEY", "k" * 12)
    registry = get_model_registry()
    registry.load_rows([_row("grok", "g1", default=True)])

    async def not_found(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ProviderTransportError("Grok API error 404: Not Found", status_code=404)

    monkeypatch.setattr(gp, "post_json", not_found)
    with pytest.raises(ProviderTransportError):
        await GrokProvider().reason(_context())
    assert registry.get("grok", "g1").status == "active"


# --------------------------------------------------------------------------
# Periodic availability poll (mock-tested: no vendor list endpoint reachable here)
# --------------------------------------------------------------------------


class _ListsModels:
    def __init__(self, offered, configured=True):
        self.offered, self._configured = offered, configured

    def is_configured(self):
        return self._configured

    async def list_available_models(self):
        return self.offered


def _use_fake_provider(monkeypatch, provider_id, fake):
    import app.providers.availability as av

    monkeypatch.setattr(av, "get_provider_strict", lambda pid: fake if pid == provider_id else (_ for _ in ()).throw(KeyError(pid)))


@pytest.mark.asyncio
async def test_poll_retires_an_in_use_model_the_vendor_no_longer_lists(monkeypatch):
    from app.providers.availability import check_provider_availability

    registry = get_model_registry()
    registry.load_rows([_row("grok", "g1", default=True), _row("grok", "g2", fallback=True), _row("grok", "g3")])
    _use_fake_provider(monkeypatch, "grok", _ListsModels({"g2", "g3"}))
    result = await check_provider_availability("grok")
    assert result.checked is True and result.retired == ["g1"]
    assert registry.default_model_id("grok") == "g2"  # the fallback took over
    assert registry.get("grok", "g3").status == "active"  # an unused model is never touched


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fake,reason_part",
    [
        (_ListsModels(None), "could not be read"),
        (_ListsModels(set()), "could not be read"),
        (_ListsModels({"x"}, configured=False), "not configured"),
        (_ListsModels({"unrelated-1", "unrelated-2"}), "none of the registry's callable models"),
    ],
)
async def test_poll_changes_nothing_when_the_evidence_is_weak(monkeypatch, fake, reason_part):
    from app.providers.availability import check_provider_availability

    registry = get_model_registry()
    registry.load_rows([_row("grok", "g1", default=True), _row("grok", "g2", fallback=True)])
    _use_fake_provider(monkeypatch, "grok", fake)
    result = await check_provider_availability("grok")
    assert result.checked is False and result.retired == [] and reason_part in result.skipped_reason
    assert registry.get("grok", "g1").status == "active"


@pytest.mark.asyncio
async def test_poll_normalises_google_style_names_and_counts_an_env_pinned_model(monkeypatch):
    from app.providers.availability import check_provider_availability

    monkeypatch.setenv("GROK_MODEL_ID", "pinned-model")
    registry = get_model_registry()
    registry.load_rows([_row("grok", "g1", default=True), _row("grok", "pinned-model")])
    _use_fake_provider(monkeypatch, "grok", _ListsModels({"models/g1", "models/other"}))
    result = await check_provider_availability("grok")
    # g1 is listed ("models/g1" -> "g1"); the env-pinned model is in use and not listed.
    assert result.retired == ["pinned-model"]


@pytest.mark.asyncio
async def test_poll_skips_a_provider_without_list_support(monkeypatch):
    from app.providers.availability import check_provider_availability

    class _NoLister:
        def is_configured(self):
            return True

    _use_fake_provider(monkeypatch, "grok", _NoLister())
    result = await check_provider_availability("grok")
    assert result.checked is False and "no model-list support" in result.skipped_reason


@pytest.mark.asyncio
async def test_a_crashing_provider_check_does_not_stop_the_other(monkeypatch):
    import app.providers.availability as av

    seen = []

    async def check(provider_id):
        seen.append(provider_id)
        if provider_id == "grok":
            raise RuntimeError("boom")
        return av.AvailabilityResult(provider_id, True, 3)

    monkeypatch.setattr(av, "check_provider_availability", check)
    results = await av.run_availability_check()
    assert seen == ["grok", "gemma"] and results[0].checked is False and "boom" in results[0].skipped_reason
    assert results[1].checked is True


def test_vendor_model_list_readers_return_none_without_credentials():
    from app.providers.gemma_provider import GemmaProvider
    from app.providers.grok_provider import GrokProvider

    assert asyncio.run(GrokProvider().list_available_models()) is None
    assert asyncio.run(GemmaProvider().list_available_models()) is None


@pytest.mark.asyncio
async def test_grok_list_reader_parses_the_openai_style_list(monkeypatch):
    import httpx

    from app.providers.grok_provider import GrokProvider

    monkeypatch.setenv("XAI_API_KEY", "k" * 12)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models") and request.headers["authorization"] == "Bearer " + "k" * 12
        return httpx.Response(200, json={"data": [{"id": "grok-4.6"}, {"id": "grok-4.3"}, {"nope": 1}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    assert await GrokProvider().list_available_models() == {"grok-4.6", "grok-4.3"}


@pytest.mark.asyncio
async def test_grok_list_reader_is_inconclusive_on_errors(monkeypatch):
    import httpx

    from app.providers.grok_provider import GrokProvider

    monkeypatch.setenv("XAI_API_KEY", "k" * 12)
    real_client = httpx.AsyncClient
    for response in (httpx.Response(500), httpx.Response(200, json={"data": []}), httpx.Response(200, text="not json")):
        monkeypatch.setattr(httpx, "AsyncClient", lambda r=response, **kw: real_client(transport=httpx.MockTransport(lambda req, r=r: r), **kw))
        assert await GrokProvider().list_available_models() is None


# --------------------------------------------------------------------------
# Response shape
# --------------------------------------------------------------------------


def test_health_response_carries_both_the_camel_and_the_specified_snake_case_fields():
    from app.schemas import HealthResponse

    body = HealthResponse(ok=True, latencyMs=12.5, modelChecked="m", detail="ok", transport="hosted_api").model_dump()
    assert (body["latency_ms"], body["model_checked"]) == (12.5, "m") == (body["latencyMs"], body["modelChecked"])


def test_trade_analysis_requested_provider_defaults_to_none():
    from app.schemas import TradeAnalysis

    assert TradeAnalysis.model_fields["requestedProvider"].default is None


def test_compare_request_accepts_the_spec_alias_and_enforces_two_or_three():
    from pydantic import ValidationError

    from app.schemas import CompareRequest

    assert CompareRequest(symbol="TCS", provider_ids=["grok", "gemma"]).providers == ["grok", "gemma"]
    assert CompareRequest(symbol="TCS", providerIds=["grok", "gemma", "custom"]).providers == ["grok", "gemma", "custom"]
    for bad in (["grok"], ["a", "b", "c", "d"], []):
        with pytest.raises(ValidationError):
            CompareRequest(symbol="TCS", providers=bad)


def test_the_cli_generates_a_usable_settings_key_without_a_database(monkeypatch, capsys):
    from app.cli import main
    from app.security import secret_store as ss

    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["generate-settings-key"]) == 0
    key = capsys.readouterr().out.strip()
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", key)
    assert ss.is_configured() is True and ss.decrypt_secret(ss.encrypt_secret("round-trip")) == "round-trip"
    assert main(["generate-settings-key"]) == 0 and capsys.readouterr().out.strip() != key  # a fresh key every time
