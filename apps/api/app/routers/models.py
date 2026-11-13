"""
Model Registry administration — Passage 1 §10 / Passage 4 §6.2:

    GET/POST/PATCH/DELETE /api/ai/models — "Drives the Model Registry admin UI (P2 §11.1) —
    CRUD over ai_models, RBAC-gated to admin role, all writes go through audit_logs."

Every route here is admin-only (including the reads: the registry is operational
configuration, and non-admins get the model names they need — the "active model" badge — from
`GET /api/ai/providers`). Every write leaves an `audit_logs` row with the before/after state, then
reloads the in-process registry so the change is effective immediately on this instance.

Two registry-wide actions sit alongside the CRUD:

  POST /api/ai/models/sync-catalogue      offer the built-in catalogue to ai_models (idempotent;
                                          `?force=true` restores rows an admin deleted)
  POST /api/ai/models/availability-check  run the vendor model-list poll now (G02g)
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status

from ..db.audit import record_audit
from ..db.models import (
    ModelConflictError,
    ModelInUseError,
    ModelNotFoundError,
    ModelRow,
    ModelValidationError,
    create_model_row,
    delete_model_row,
    get_model_row,
    list_model_rows,
    update_model_row,
)
from ..errors import ConflictError, NotFoundError, ValidationError
from ..providers.availability import run_availability_check
from ..providers.registry import VALID_PROVIDER_IDS
from ..providers.runtime_config import reload_models, sync_catalogue
from ..schemas import ModelCreateRequest, ModelResponse, ModelUpdateRequest
from ..security.dependencies import AuthenticatedUser, require_role

router = APIRouter(prefix="/api/ai/models", tags=["models"])

_FIELD_MAP = {
    "displayName": "display_name",
    "status": "status",
    "contextWindow": "context_window",
    "inputModalities": "input_modalities",
    "outputModalities": "output_modalities",
    "isDefault": "is_default",
    "isFallback": "is_fallback",
    "onlineCapable": "online_capable",
    "offlineCapable": "offline_capable",
    "config": "config",
}


def _view(row: ModelRow) -> ModelResponse:
    return ModelResponse(
        id=row.id,
        providerId=row.provider,
        modelId=row.model_id,
        displayName=row.display_name,
        status=row.status,  # type: ignore[arg-type]
        contextWindow=row.context_window,
        inputModalities=row.input_modalities,
        outputModalities=row.output_modalities,
        isDefault=row.is_default,
        isFallback=row.is_fallback,
        onlineCapable=row.online_capable,
        offlineCapable=row.offline_capable,
        config=row.config,
        createdAt=row.created_at.isoformat() if row.created_at else None,
        updatedAt=row.updated_at.isoformat() if row.updated_at else None,
    )


def _audit_state(row: ModelRow) -> dict:
    return {
        "provider": row.provider,
        "modelId": row.model_id,
        "displayName": row.display_name,
        "status": row.status,
        "isDefault": row.is_default,
        "isFallback": row.is_fallback,
        "contextWindow": row.context_window,
        "onlineCapable": row.online_capable,
        "offlineCapable": row.offline_capable,
        "config": row.config,
    }


def _model_uuid(raw: str) -> str:
    try:
        return str(uuid.UUID(raw))
    except ValueError as err:
        raise NotFoundError(f"No model with id '{raw}'") from err


def _translate(err: Exception) -> Exception:
    if isinstance(err, ModelNotFoundError):
        return NotFoundError(str(err))
    if isinstance(err, ModelInUseError):
        return ConflictError(str(err), details={"references": err.references})
    if isinstance(err, ModelConflictError):
        return ConflictError(str(err))
    if isinstance(err, ModelValidationError):
        return ValidationError(str(err))
    return err


@router.get("", response_model=list[ModelResponse])
async def list_models(
    provider: str | None = Query(default=None, description="Filter by provider id"),
    _admin: AuthenticatedUser = Depends(require_role("admin")),
) -> list[ModelResponse]:
    if provider is not None and provider not in VALID_PROVIDER_IDS:
        raise ValidationError(f"Unknown provider '{provider}'")
    return [_view(r) for r in await list_model_rows(provider)]


@router.post("/sync-catalogue")
async def sync_model_catalogue(
    force: bool = Query(default=False, description="Offer every built-in entry again, restoring deleted rows"),
    admin: AuthenticatedUser = Depends(require_role("admin")),
) -> dict:
    offered = await sync_catalogue(force=force)
    await record_audit(
        actor_id=admin.id,
        action="model.sync_catalogue",
        resource="ai_models",
        after={"force": force, "offered": offered},
    )
    await reload_models()
    return {"offered": offered, "force": force}


@router.post("/availability-check")
async def availability_check(admin: AuthenticatedUser = Depends(require_role("admin"))) -> dict:
    results = await run_availability_check()
    retired = [f"{r.provider_id}:{m}" for r in results for m in r.retired]
    await record_audit(
        actor_id=admin.id,
        action="model.availability_check",
        resource="ai_models",
        after={"retired": retired},
    )
    return {
        "results": [
            {
                "providerId": r.provider_id,
                "checked": r.checked,
                "modelsOffered": r.models_offered,
                "retired": r.retired,
                "skippedReason": r.skipped_reason,
            }
            for r in results
        ]
    }


@router.get("/{model_uuid}", response_model=ModelResponse)
async def get_model(
    model_uuid: str, _admin: AuthenticatedUser = Depends(require_role("admin"))
) -> ModelResponse:
    row = await get_model_row(_model_uuid(model_uuid))
    if row is None:
        raise NotFoundError(f"No model with id '{model_uuid}'")
    return _view(row)


@router.post("", response_model=ModelResponse, status_code=status.HTTP_201_CREATED)
async def create_model(
    body: ModelCreateRequest, admin: AuthenticatedUser = Depends(require_role("admin"))
) -> ModelResponse:
    if body.providerId not in VALID_PROVIDER_IDS:
        raise ValidationError(f"Unknown provider '{body.providerId}'")
    try:
        row = await create_model_row(
            body.providerId,
            body.modelId.strip(),
            body.displayName.strip(),
            status=body.status,
            context_window=body.contextWindow,
            input_modalities=body.inputModalities,
            output_modalities=body.outputModalities,
            is_default=body.isDefault,
            is_fallback=body.isFallback,
            online_capable=body.onlineCapable,
            offline_capable=body.offlineCapable,
            config=body.config,
        )
    except (ModelValidationError, ModelConflictError) as err:
        raise _translate(err) from err
    await record_audit(
        actor_id=admin.id, action="model.create", resource=f"ai_model:{row.id}", after=_audit_state(row)
    )
    await reload_models()
    return _view(row)


@router.patch("/{model_uuid}", response_model=ModelResponse)
async def update_model(
    model_uuid: str,
    body: ModelUpdateRequest,
    admin: AuthenticatedUser = Depends(require_role("admin")),
) -> ModelResponse:
    model_uuid = _model_uuid(model_uuid)
    changes = {
        _FIELD_MAP[name]: getattr(body, name) for name in body.model_fields_set if name in _FIELD_MAP
    }
    if not changes:
        raise ValidationError("No updatable fields were provided")
    # `status`, `isDefault`, `isFallback`, `displayName` cannot be cleared to null.
    for name in ("status", "isDefault", "isFallback", "displayName", "onlineCapable", "offlineCapable"):
        if name in body.model_fields_set and getattr(body, name) is None:
            raise ValidationError(f"'{name}' cannot be null")
    try:
        before, after = await update_model_row(model_uuid, **changes)
    except (ModelNotFoundError, ModelValidationError, ModelConflictError) as err:
        raise _translate(err) from err
    await record_audit(
        actor_id=admin.id,
        action="model.update",
        resource=f"ai_model:{after.id}",
        before=_audit_state(before),
        after=_audit_state(after),
    )
    await reload_models()
    return _view(after)


@router.delete("/{model_uuid}")
async def delete_model(
    model_uuid: str, admin: AuthenticatedUser = Depends(require_role("admin"))
) -> dict:
    model_uuid = _model_uuid(model_uuid)
    try:
        row = await delete_model_row(model_uuid)
    except (ModelNotFoundError, ModelInUseError) as err:
        raise _translate(err) from err
    await record_audit(
        actor_id=admin.id, action="model.delete", resource=f"ai_model:{row.id}", before=_audit_state(row)
    )
    await reload_models()
    return {"ok": True, "id": row.id}
