"""
Runtime configuration — what the database controls, loaded into the running process.

Three admin-managed things live in Postgres and are consulted on the hot path from memory:

* the model registry (`ai_models`) — which model is each provider's default / fallback, and
  which are retired or disabled (Passage 1 §4.5, §9.1);
* per-provider settings and flags (`ai_provider_settings`, `ai_providers.enabled/priority`) —
  API-key override, base URL, timeout, retries (Passage 4 §5.2);
* the circuit-breaker thresholds (`system_settings['circuit_breaker']`, Passage 4 §3.3 / G03).

`reload_runtime_config()` runs at startup (with catalogue seeding) and every
`RUNTIME_CONFIG_REFRESH_SECONDS` thereafter, and the process that handles an admin write reloads
immediately — so a change is effective at once on that instance and within one refresh interval
on any other. (A shared pub/sub invalidation is the Redis phase.) Every section is independent:
a failure in one is recorded in the returned report and logged, and the process simply keeps
serving from the last good in-memory state or, at first boot, from the built-in defaults.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from ..config import CircuitBreakerSettings, get_settings
from ..db.client import is_db_configured
from ..observability import get_logger
from ..security.secret_store import SecretStoreError, decrypt_secret
from .circuit_breaker import get_circuit_breakers
from .model_registry import get_model_registry
from .provider_settings import ProviderOverrides, set_overrides, set_provider_flags

logger = get_logger("providers.runtime_config")

SEEDED_KEY = "model_catalogue_seeded"
BREAKER_KEY = "circuit_breaker"

# The catalogue's "custom-configured" entry is a display placeholder, not a model.
_PLACEHOLDER_MODEL_IDS = frozenset({("custom", "custom-configured")})


def _catalogue_seed_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for e in get_model_registry().catalogue_entries():
        if (e.provider_id, e.model_id) in _PLACEHOLDER_MODEL_IDS:
            continue
        config: dict[str, Any] = {"routableForAnalysis": e.routable_for_analysis}
        if e.redirects_to:
            config["redirectsTo"] = e.redirects_to
        if e.notes:
            config["notes"] = e.notes
        entries.append(
            {
                "provider": e.provider_id,
                "model_id": e.model_id,
                "display_name": e.display_name,
                "status": e.status,
                "context_window": e.context_window,
                "input_modalities": e.input_modalities,
                "output_modalities": e.output_modalities,
                "online_capable": e.online_capable,
                "offline_capable": e.offline_capable,
                "is_default": e.is_default,
                "is_fallback": e.is_fallback,
                "config": config,
            }
        )
    # Custom AI has no vendor catalogue; the model the owner configured through the environment
    # is the one real entry it can have.
    custom_model = os.environ.get("CUSTOM_AI_MODEL_ID", "").strip()
    if custom_model:
        entries.append(
            {
                "provider": "custom",
                "model_id": custom_model,
                "display_name": custom_model,
                "status": "active",
                "online_capable": True,
                "offline_capable": True,
                "is_default": True,
                "is_fallback": False,
                "config": {"notes": "Configured through CUSTOM_AI_MODEL_ID."},
            }
        )
    return entries


async def sync_catalogue(*, force: bool = False) -> list[str]:
    """
    Offer the built-in catalogue to `ai_models`: insert what was never offered before and
    remember what was (system_settings['model_catalogue_seeded']), so an admin who deletes a
    seeded row does not see it reappear at the next restart, while a catalogue entry added by a
    later release does appear. `force=True` (an explicit admin action) offers everything again,
    restoring deleted rows; existing rows are never modified either way. Returns the newly offered
    "provider:model" keys.
    """
    from ..db.models import seed_missing
    from ..db.system_settings import get_setting, set_setting

    already = set(await get_setting(SEEDED_KEY) or [])
    offered = await seed_missing(_catalogue_seed_entries(), set() if force else already)
    if offered:
        await set_setting(SEEDED_KEY, sorted(already | set(offered)), None)
        logger.info("model catalogue offered to ai_models", extra={"newModels": len(offered)})
    return offered


async def reload_models() -> int:
    from ..db.models import list_model_rows

    rows = await list_model_rows()
    if not rows:
        # An empty table must not blank the registry (first boot before seeding, or a wiped DB).
        return 0
    get_model_registry().load_rows(rows)
    return len(rows)


async def reload_provider_settings() -> int:
    from ..db.provider_settings import list_provider_flags, list_provider_settings

    mapping: dict[str, ProviderOverrides] = {}
    for row in await list_provider_settings():
        key: str | None = None
        key_error: str | None = None
        if row.api_key_encrypted:
            try:
                key = decrypt_secret(row.api_key_encrypted)
            except SecretStoreError as err:
                # Keep serving from the environment; make the problem visible to admins.
                key_error = str(err)
                logger.error("stored provider API key is unusable", extra={"provider": row.provider})
        mapping[row.provider] = ProviderOverrides(
            api_key=key,
            base_url=row.base_url,
            timeout_ms=row.timeout_ms,
            retry_count=row.retry_count,
            region=row.region,
            key_error=key_error,
            key_stored=bool(row.api_key_encrypted),
        )
    set_overrides(mapping)
    flags = await list_provider_flags()
    set_provider_flags({name: (bool(f["enabled"]), int(f["priority"])) for name, f in flags.items()})
    return len(mapping)


def parse_breaker_config(
    value: Any,
) -> tuple[CircuitBreakerSettings, dict[str, CircuitBreakerSettings]]:
    """
    `system_settings['circuit_breaker']` -> (default, per-provider). Any field the stored value
    omits falls back to the environment default (CB_*), so an admin only stores what they changed.
    Out-of-range stored values are ignored field by field rather than disabling the breaker.
    """
    base = get_settings().circuit_breaker
    value = value if isinstance(value, dict) else {}

    def merged(parent: CircuitBreakerSettings, raw: Any) -> CircuitBreakerSettings:
        raw = raw if isinstance(raw, dict) else {}

        def pick(name: str, current: float, lo: float, hi: float) -> float:
            candidate = raw.get(name)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and lo <= candidate <= hi:
                return float(candidate)
            return current

        return CircuitBreakerSettings(
            failure_threshold=int(pick("failureThreshold", parent.failure_threshold, 1, 100)),
            window_seconds=pick("windowSeconds", parent.window_seconds, 1, 3600),
            cooldown_seconds=pick("cooldownSeconds", parent.cooldown_seconds, 1, 3600),
        )

    default = merged(base, value)
    per_provider = {
        str(name): merged(default, raw)
        for name, raw in (value.get("providers") or {}).items()
        if isinstance(raw, dict)
    }
    return default, per_provider


async def reload_breaker_settings() -> None:
    from ..db.system_settings import get_setting

    default, per_provider = parse_breaker_config(await get_setting(BREAKER_KEY))
    get_circuit_breakers().apply_settings(default, per_provider)


async def reload_runtime_config(*, seed: bool = False) -> dict[str, Any]:
    """Reload everything the database controls. Never raises; the report says what happened."""
    report: dict[str, Any] = {"models": 0, "providerSettings": 0, "seeded": [], "errors": []}
    if not is_db_configured():
        return report
    steps: list[tuple[str, Any]] = []
    if seed:
        steps.append(("catalogue", sync_catalogue))
    steps += [
        ("models", reload_models),
        ("providerSettings", reload_provider_settings),
        ("circuitBreaker", reload_breaker_settings),
    ]
    for name, step in steps:
        try:
            result = await step()
        except Exception as err:  # noqa: BLE001 — each section stands alone
            report["errors"].append(f"{name}: {type(err).__name__}: {err}")
            logger.error("runtime config reload failed", extra={"section": name, "error": str(err)})
            continue
        if name == "catalogue":
            report["seeded"] = result
        elif name in ("models", "providerSettings"):
            report[name] = result
    return report


async def runtime_config_loop(interval_seconds: float) -> None:
    """Re-reads the database every `interval_seconds` until cancelled (startup already loaded once)."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await reload_runtime_config()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.warning("runtime config refresh crashed", extra={"error": str(err)})
