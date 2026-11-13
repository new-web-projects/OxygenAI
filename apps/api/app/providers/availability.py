"""
Periodic model-availability check — Passage 4 §3.2 (G02g):

    "build an admin job that periodically calls the models endpoint ... and flips status
     automatically, with an alert, rather than relying on manual upkeep. This applies identically
     to Gemma's model list (P1 §4.3) once Google revises its own model IDs."

The other half of that sentence — catching a 4xx "model not found" on a real call — is
`providers/lifecycle.py` and is always on. This is the poll: ask each hosted vendor which models
this account can call, and retire the ones the router is *using* (a provider's default / fallback,
or the model an environment variable pins) that are no longer offered.

Absence from a list is weaker evidence than an explicit 404, so the poll is conservative:

* a list that cannot be read, or comes back empty, changes nothing (`None` = inconclusive);
* only models the router actually uses are retired — the long tail of catalogue entries nobody
  selected is left alone;
* if NONE of the provider's callable catalogue models appears in the list, the list is assumed to
  describe something else (wrong account, wrong project, a filtered view) and nothing is retired.

Mock-tested only: neither vendor list endpoint could be called from this environment (no API keys,
no network). The classification and retirement logic are exercised against fake provider lists; the
two `list_available_models()` implementations are the unverified edge.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..observability import get_logger
from .lifecycle import retire_model
from .model_registry import DEFAULT_ELIGIBLE, FALLBACK_ELIGIBLE, configured_model_id, get_model_registry
from .registry import get_provider_strict

logger = get_logger("providers.availability")

CHECKED_PROVIDERS = ("grok", "gemma")


@dataclass
class AvailabilityResult:
    provider_id: str
    checked: bool
    models_offered: int | None = None
    retired: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


def _normalise(model_id: str) -> str:
    return model_id.split("/")[-1]


async def check_provider_availability(provider_id: str) -> AvailabilityResult:
    provider = get_provider_strict(provider_id)
    lister = getattr(provider, "list_available_models", None)
    if lister is None:
        return AvailabilityResult(provider_id, False, skipped_reason="the provider has no model-list support")
    if not provider.is_configured():
        return AvailabilityResult(provider_id, False, skipped_reason="the provider is not configured")

    offered = await lister()
    if not offered:
        return AvailabilityResult(
            provider_id, False, skipped_reason="the vendor's model list could not be read (inconclusive)"
        )
    offered_ids = {_normalise(m) for m in offered}

    registry = get_model_registry()
    callable_models = {
        e.model_id
        for e in registry.all(provider_id)
        if e.routable_for_analysis and e.status in set(DEFAULT_ELIGIBLE) | set(FALLBACK_ELIGIBLE)
    }
    if callable_models and not (callable_models & offered_ids):
        logger.warning(
            "vendor model list shares no model with the registry; treating it as inconclusive",
            extra={"provider": provider_id},
        )
        return AvailabilityResult(
            provider_id,
            False,
            models_offered=len(offered_ids),
            skipped_reason=(
                "none of the registry's callable models appears in the vendor's list — "
                "assuming the list describes something else and changing nothing"
            ),
        )

    in_use = {
        e.model_id
        for e in registry.all(provider_id)
        if (e.is_default or e.is_fallback)
        and e.routable_for_analysis
        and e.status in set(DEFAULT_ELIGIBLE) | set(FALLBACK_ELIGIBLE)
    }
    in_use.add(configured_model_id(provider_id))  # an env-pinned model counts as in use
    in_use.discard("unset")

    result = AvailabilityResult(provider_id, True, models_offered=len(offered_ids))
    for model_id in sorted(in_use):
        if model_id in offered_ids:
            continue
        outcome = await retire_model(
            provider_id,
            model_id,
            "no longer offered by the vendor's model list",
            detected_by="availability_check",
        )
        if outcome.changed:
            result.retired.append(model_id)
    return result


async def run_availability_check() -> list[AvailabilityResult]:
    results: list[AvailabilityResult] = []
    for provider_id in CHECKED_PROVIDERS:
        try:
            results.append(await check_provider_availability(provider_id))
        except Exception as err:  # noqa: BLE001 — one provider must not stop the other
            logger.warning("availability check failed", extra={"provider": provider_id, "error": str(err)})
            results.append(AvailabilityResult(provider_id, False, skipped_reason=f"{type(err).__name__}: {err}"))
    return results


async def availability_loop(interval_seconds: float, initial_delay_seconds: float = 60.0) -> None:
    """Runs the poll after an initial delay (never during startup), then every interval, until cancelled."""
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            await run_availability_check()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            logger.warning("availability poll crashed", extra={"error": str(err)})
        await asyncio.sleep(interval_seconds)
