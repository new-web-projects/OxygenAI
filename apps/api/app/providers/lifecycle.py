"""
Model lifecycle — Passage 4 §3.2 (G02g), the "catch a 4xx 'model not found'" half:

    "build an admin job that periodically calls the models endpoint (or catches a 4xx
     'model not found') and flips status automatically, with an alert, rather than
     relying on manual upkeep. This applies identically to Gemma's model list."

`retire_model()` is what that sentence needs and `providers/availability.py` (the periodic
half) reuses it: flip the model to `retired` in memory and in `ai_models`, let the provider's
fallback take over as default if this was the default, and leave a durable alert (an
`error_logs` row — the Error Center's feed — plus an `audit_logs` row with a null actor, since
no person did it). It is idempotent: a model already retired produces no second alert.

Classification is conservative on purpose (`is_model_not_found`): a wrong "retired" verdict
would take a working model out of service and persist that decision, so a 404 caused by a
mistyped base URL, a 400 about some other parameter, or a network error that merely contains
the word "model" must not trigger it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from ..db.client import is_db_configured
from ..observability import get_logger
from .model_health import get_model_health
from .model_registry import configured_model_id, get_model_registry

logger = get_logger("providers.lifecycle")

_MISSING_PHRASES = (
    "not found",
    "does not exist",
    "doesn't exist",
    "no longer available",
    "no longer supported",
    "has been retired",
    "has been deprecated",
    "has been decommissioned",
    "model_not_found",
    "unknown model",
    "not a valid model",
)


def is_model_not_found(status_code: int | None, message: str, model_id: str | None = None) -> bool:
    """
    True only when the failure is evidently about the MODEL being gone.

    * HTTP 404 whose body talks about a model.
    * HTTP 400/410 whose body talks about a model AND says it is missing/retired.
    * No HTTP status (an SDK exception): the text must name this very model id and say it is
      missing — "model server host not found" must not retire anything.
    """
    lowered = (message or "").lower()
    if "model" not in lowered:
        return False
    if status_code == 404:
        return True
    says_missing = any(phrase in lowered for phrase in _MISSING_PHRASES)
    if status_code in (400, 410):
        return says_missing
    if status_code is None:
        return says_missing and bool(model_id) and model_id.lower() in lowered  # type: ignore[union-attr]
    return False


@dataclass
class RetirementOutcome:
    provider_id: str
    model_id: str
    changed: bool
    was_in_use: bool
    replacement: str | None
    persisted: bool


async def retire_model(
    provider_id: str, model_id: str, reason: str, *, detected_by: str = "runtime"
) -> RetirementOutcome:
    registry = get_model_registry()
    entry = registry.get(provider_id, model_id)
    if entry is not None and entry.status == "retired":
        return RetirementOutcome(provider_id, model_id, False, False, None, False)

    previous_status = entry.status if entry else None
    was_in_use = model_id == configured_model_id(provider_id)
    registry.mark_model_retired(provider_id, model_id, reason)
    get_model_health().reset(provider_id, model_id)
    replacement = registry.default_model_id(provider_id)

    persisted = False
    if is_db_configured():
        # Imported here: db modules import the registry, and the registry must not import them.
        from ..db.audit import record_audit
        from ..db.errors import record_error
        from ..db.models import retire_model_row

        try:
            await retire_model_row(provider_id, model_id, reason, entry.redirects_to if entry else None)
            persisted = True
        except Exception as err:  # noqa: BLE001 — the in-memory flip already protects this process
            logger.error(
                "could not persist a model retirement",
                extra={"provider": provider_id, "model": model_id, "error": str(err)},
            )
        outcome_text = (
            f"'{replacement}' is now the default" if replacement else "the provider now has NO usable default model"
        )
        await record_error(
            error_id=uuid.uuid4().hex[:12],
            error_type="ModelRetired",
            message=f"{provider_id}:{model_id} was marked retired ({detected_by}): {reason[:300]}. {outcome_text}.",
            provider=provider_id,
            severity="high" if was_in_use else "medium",
        )
        await record_audit(
            actor_id=None,
            action="model.auto_retire",
            resource=f"ai_model:{provider_id}:{model_id}",
            before={"status": previous_status},
            after={"status": "retired", "detectedBy": detected_by, "replacement": replacement, "reason": reason[:300]},
        )
        if persisted:
            from .runtime_config import reload_models

            await reload_models()
    logger.warning(
        "model retired automatically",
        extra={"provider": provider_id, "model": model_id, "replacement": replacement, "persisted": persisted},
    )
    return RetirementOutcome(provider_id, model_id, True, was_in_use, replacement, persisted)
