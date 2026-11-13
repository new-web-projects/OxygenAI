"""
`ai_models` — the model registry's system of record (Passage 1 §4.5 / §9.1).

Pure persistence plus the invariants the database cannot express alone:

* at most one default and one fallback model per provider (partial unique indexes,
  migration 0007), a model is never both, and only models the router may actually
  call can be either — validated here first so the caller gets a precise message
  instead of a constraint-violation string;
* retiring / disabling / deprecating a model clears its default/fallback flags and,
  if that leaves the provider without a default, the provider's fallback is
  promoted (`_repair_defaults`) — an explicit, logged rule rather than leaving the
  provider with no model at all;
* a model that has history is never hard-deleted: every foreign key to `ai_models`
  is ON DELETE SET NULL, so deleting would silently strip the model attribution
  from the evaluation dataset (`trading_signals.generated_by`, comparison results,
  usage). The admin is told to retire it instead (`ModelInUseError`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import asyncpg

from ..observability import get_logger
from .client import get_pool
from .signals import PROVIDER_TYPE

logger = get_logger("db.models")

STATUSES = ("active", "disabled", "deprecated", "retired", "local", "hosted", "fallback")
# Statuses the router may select as a provider's default model / its fallback model —
# defined once, in the registry, so the two cannot drift.
from ..providers.model_registry import DEFAULT_ELIGIBLE, FALLBACK_ELIGIBLE  # noqa: E402

# Every (table, column) with a foreign key to ai_models(id). A test asserts this list
# against information_schema so a future migration cannot add one unnoticed.
MODEL_REFERENCES: tuple[tuple[str, str], ...] = (
    ("trading_signals", "generated_by"),
    ("messages", "model_id"),
    ("ai_comparison_results", "model_id"),
    ("ai_health_checks", "model_id"),
    ("usage_logs", "model_id"),
    ("ai_fallback_rules", "fallback_model_id"),
)


class ModelError(Exception):
    """Base for registry-state errors the API maps to 4xx."""


class ModelValidationError(ModelError):
    pass


class ModelConflictError(ModelError):
    pass


class ModelNotFoundError(ModelError):
    pass


class ModelInUseError(ModelError):
    def __init__(self, references: dict[str, int]) -> None:
        self.references = references
        total = sum(references.values())
        super().__init__(
            f"This model has history ({total} referencing row(s): "
            + ", ".join(f"{t}={n}" for t, n in references.items())
            + "). Deleting it would strip model attribution from the evaluation dataset — "
            "set its status to 'retired' instead."
        )


@dataclass
class ModelRow:
    id: str
    provider: str
    provider_db_id: str
    model_id: str
    display_name: str
    status: str
    context_window: int | None
    input_modalities: list[str]
    output_modalities: list[str]
    is_default: bool
    is_fallback: bool
    online_capable: bool
    offline_capable: bool
    config: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


_SELECT = """
SELECT m.id, p.name AS provider, m.provider_id, m.model_id, m.display_name, m.status,
       m.context_window, m.input_modalities, m.output_modalities, m.is_default, m.is_fallback,
       m.online_capable, m.offline_capable, m.config, m.created_at, m.updated_at
  FROM ai_models m JOIN ai_providers p ON p.id = m.provider_id
"""


def _row(record: asyncpg.Record) -> ModelRow:
    config = record["config"]
    return ModelRow(
        id=str(record["id"]),
        provider=record["provider"],
        provider_db_id=str(record["provider_id"]),
        model_id=record["model_id"],
        display_name=record["display_name"],
        status=record["status"],
        context_window=record["context_window"],
        input_modalities=list(record["input_modalities"] or []),
        output_modalities=list(record["output_modalities"] or []),
        is_default=record["is_default"],
        is_fallback=record["is_fallback"],
        online_capable=record["online_capable"],
        offline_capable=record["offline_capable"],
        config=json.loads(config) if isinstance(config, str) else dict(config or {}),
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


def validate_state(status: str, is_default: bool, is_fallback: bool) -> None:
    """The registry's invariants, as precise messages. Raises ModelValidationError."""
    if status not in STATUSES:
        raise ModelValidationError(f"status must be one of {', '.join(STATUSES)}")
    if is_default and is_fallback:
        raise ModelValidationError("A model cannot be both a provider's default and its fallback")
    if is_default and status not in DEFAULT_ELIGIBLE:
        raise ModelValidationError(
            f"A '{status}' model cannot be the default; only {', '.join(DEFAULT_ELIGIBLE)} models can"
        )
    if is_fallback and status not in FALLBACK_ELIGIBLE:
        raise ModelValidationError(
            f"A '{status}' model cannot be the fallback; only {', '.join(FALLBACK_ELIGIBLE)} models can"
        )


async def list_model_rows(provider: str | None = None) -> list[ModelRow]:
    pool = await get_pool()
    if provider:
        records = await pool.fetch(_SELECT + " WHERE p.name = $1 ORDER BY p.name, m.model_id", provider)
    else:
        records = await pool.fetch(_SELECT + " ORDER BY p.name, m.model_id")
    return [_row(r) for r in records]


async def get_model_row(model_uuid: str) -> ModelRow | None:
    pool = await get_pool()
    try:
        record = await pool.fetchrow(_SELECT + " WHERE m.id = $1::uuid", model_uuid)
    except asyncpg.DataError:  # not a uuid
        return None
    return _row(record) if record is not None else None


async def _provider_db_id(conn: asyncpg.Connection, provider: str) -> str:
    record = await conn.fetchrow(
        """
        INSERT INTO ai_providers (name, type) VALUES ($1, $2)
        ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id
        """,
        provider,
        PROVIDER_TYPE.get(provider, provider),
    )
    return str(record["id"])


async def _clear_flag(conn: asyncpg.Connection, provider_db_id: str, flag: str, except_id: str | None) -> None:
    assert flag in ("is_default", "is_fallback")
    await conn.execute(
        f"UPDATE ai_models SET {flag} = false, updated_at = now() "
        f"WHERE provider_id = $1 AND {flag} AND ($2::uuid IS NULL OR id <> $2::uuid)",
        provider_db_id,
        except_id,
    )


async def _repair_defaults(conn: asyncpg.Connection, provider_db_id: str) -> str | None:
    """
    If the provider has no default left, promote its fallback (clearing that
    fallback slot). Returns the promoted model's id, or None when nothing changed.
    """
    has_default = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM ai_models WHERE provider_id = $1 AND is_default)", provider_db_id
    )
    if has_default:
        return None
    # A model that becomes the default is, by definition, no longer "callable only as the
    # fallback": status 'fallback' becomes 'active' (the CHECK constraint forbids a default
    # with status 'fallback'); any other callable status is kept as it is.
    promoted = await conn.fetchrow(
        """
        UPDATE ai_models
           SET is_default = true, is_fallback = false, updated_at = now(),
               status = CASE WHEN status = 'fallback' THEN 'active' ELSE status END
         WHERE id = (SELECT id FROM ai_models
                      WHERE provider_id = $1 AND is_fallback
                        AND status IN ('active', 'hosted', 'local', 'fallback')
                      LIMIT 1)
        RETURNING id, model_id
        """,
        provider_db_id,
    )
    if promoted is not None:
        logger.warning(
            "provider lost its default model; its fallback was promoted",
            extra={"model": promoted["model_id"]},
        )
        return str(promoted["id"])
    return None


async def create_model_row(
    provider: str,
    model_id: str,
    display_name: str,
    *,
    status: str = "active",
    context_window: int | None = None,
    input_modalities: list[str] | None = None,
    output_modalities: list[str] | None = None,
    is_default: bool = False,
    is_fallback: bool = False,
    online_capable: bool = True,
    offline_capable: bool = False,
    config: dict[str, Any] | None = None,
) -> ModelRow:
    validate_state(status, is_default, is_fallback)
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                provider_db_id = await _provider_db_id(conn, provider)
                if is_default:
                    await _clear_flag(conn, provider_db_id, "is_default", None)
                if is_fallback:
                    await _clear_flag(conn, provider_db_id, "is_fallback", None)
                record = await conn.fetchrow(
                    """
                    INSERT INTO ai_models
                      (provider_id, model_id, display_name, status, context_window, input_modalities,
                       output_modalities, is_default, is_fallback, online_capable, offline_capable, config)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
                    RETURNING id
                    """,
                    provider_db_id,
                    model_id,
                    display_name,
                    status,
                    context_window,
                    input_modalities if input_modalities is not None else ["text"],
                    output_modalities if output_modalities is not None else ["text"],
                    is_default,
                    is_fallback,
                    online_capable,
                    offline_capable,
                    json.dumps(config or {}),
                )
    except asyncpg.UniqueViolationError as err:
        raise ModelConflictError(f"Model '{model_id}' already exists for provider '{provider}'") from err
    row = await get_model_row(str(record["id"]))
    assert row is not None
    return row


_UNSET: Any = object()


async def update_model_row(model_uuid: str, **changes: Any) -> tuple[ModelRow, ModelRow]:
    """
    Partial update. Accepted keys: display_name, status, context_window, input_modalities,
    output_modalities, is_default, is_fallback, online_capable, offline_capable, config.
    Returns (before, after). `config` is MERGED into the stored object, not replaced.
    """
    allowed = {
        "display_name", "status", "context_window", "input_modalities", "output_modalities",
        "is_default", "is_fallback", "online_capable", "offline_capable", "config",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unsupported fields: {sorted(unknown)}")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(_SELECT + " WHERE m.id = $1::uuid FOR UPDATE OF m", model_uuid)
            if current is None:
                raise ModelNotFoundError(f"No model with id '{model_uuid}'")
            before = _row(current)

            status = changes.get("status", before.status)
            is_default = changes.get("is_default", before.is_default)
            is_fallback = changes.get("is_fallback", before.is_fallback)
            # Making the current fallback the default is one natural step: the model leaves the
            # fallback slot as it takes the default one (unless the caller said otherwise).
            if is_default and "is_default" in changes and "is_fallback" not in changes and before.is_fallback:
                is_fallback = False
            # Same idea for status: a model whose status is 'fallback' ("callable only as the
            # fallback") that is explicitly made the default becomes 'active', as it does when it
            # is promoted automatically — unless the caller set a status in the same request.
            if is_default and "is_default" in changes and "status" not in changes and status == "fallback":
                status = "active"
            # The reverse is a role swap and is not done implicitly: demoting the default to
            # fallback would leave the provider without a default for the repair rule to fix.
            if is_fallback and before.is_default and "is_fallback" in changes:
                raise ModelValidationError(
                    "This model is the provider's default; make another model the default first, "
                    "then assign this one as the fallback"
                )
            # Moving a model out of the callable statuses demotes it automatically (one
            # call retires the default model); asking for both at once is a contradiction.
            demoted = False
            if status not in DEFAULT_ELIGIBLE and is_default and "is_default" not in changes:
                is_default, demoted = False, True
            if status not in FALLBACK_ELIGIBLE and is_fallback and "is_fallback" not in changes:
                is_fallback, demoted = False, True
            validate_state(status, bool(is_default), bool(is_fallback))

            if is_default and not before.is_default:
                await _clear_flag(conn, before.provider_db_id, "is_default", model_uuid)
            if is_fallback and not before.is_fallback:
                await _clear_flag(conn, before.provider_db_id, "is_fallback", model_uuid)

            sets: list[str] = ["updated_at = now()"]
            args: list[Any] = [model_uuid]

            def add(column: str, value: Any, cast: str = "") -> None:
                args.append(value)
                sets.append(f"{column} = ${len(args)}{cast}")

            for column in ("display_name", "context_window", "online_capable", "offline_capable"):
                if column in changes:
                    add(column, changes[column])
            if "input_modalities" in changes:
                add("input_modalities", changes["input_modalities"])
            if "output_modalities" in changes:
                add("output_modalities", changes["output_modalities"])
            add("status", status)
            add("is_default", bool(is_default))
            add("is_fallback", bool(is_fallback))
            if "config" in changes and changes["config"] is not None:
                add("config", json.dumps({**before.config, **changes["config"]}), "::jsonb")

            try:
                await conn.execute(f"UPDATE ai_models SET {', '.join(sets)} WHERE id = $1::uuid", *args)
            except asyncpg.UniqueViolationError as err:
                raise ModelConflictError(
                    "Another request changed this provider's default/fallback model at the same time; retry"
                ) from err
            if before.is_default and not is_default:
                await _repair_defaults(conn, before.provider_db_id)
    after = await get_model_row(model_uuid)
    assert after is not None
    if demoted:
        logger.info("model demoted from default/fallback by its status change", extra={"model": before.model_id})
    return before, after


async def reference_counts(conn: asyncpg.Connection, model_uuid: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, column in MODEL_REFERENCES:
        n = await conn.fetchval(f"SELECT count(*) FROM {table} WHERE {column} = $1::uuid", model_uuid)
        if n:
            counts[table] = int(n)
    return counts


async def delete_model_row(model_uuid: str) -> ModelRow:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(_SELECT + " WHERE m.id = $1::uuid FOR UPDATE OF m", model_uuid)
            if current is None:
                raise ModelNotFoundError(f"No model with id '{model_uuid}'")
            row = _row(current)
            references = await reference_counts(conn, model_uuid)
            if references:
                raise ModelInUseError(references)
            await conn.execute("DELETE FROM ai_models WHERE id = $1::uuid", model_uuid)
            if row.is_default:
                await _repair_defaults(conn, row.provider_db_id)
    return row


async def retire_model_row(
    provider: str, model_id: str, reason: str, redirects_to: str | None
) -> tuple[ModelRow | None, ModelRow | None]:
    """
    Used by the automatic lifecycle (a vendor said the model is gone). Creates the row if the
    model was never recorded. Returns (before, after); before is None for a newly recorded row.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            provider_db_id = await _provider_db_id(conn, provider)
            current = await conn.fetchrow(
                _SELECT + " WHERE p.name = $1 AND m.model_id = $2 FOR UPDATE OF m", provider, model_id
            )
            patch = {"retiredReason": reason[:500], **({"redirectsTo": redirects_to} if redirects_to else {})}
            if current is None:
                record = await conn.fetchrow(
                    """
                    INSERT INTO ai_models (provider_id, model_id, display_name, status, config)
                    VALUES ($1, $2, $2, 'retired', $3::jsonb) RETURNING id
                    """,
                    provider_db_id,
                    model_id,
                    json.dumps(patch),
                )
                before = None
                model_uuid = str(record["id"])
            else:
                before = _row(current)
                model_uuid = before.id
                if before.status != "retired":
                    await conn.execute(
                        """
                        UPDATE ai_models
                           SET status = 'retired', is_default = false, is_fallback = false,
                               config = config || $2::jsonb, updated_at = now()
                         WHERE id = $1::uuid
                        """,
                        model_uuid,
                        json.dumps(patch),
                    )
                    if before.is_default:
                        await _repair_defaults(conn, provider_db_id)
    return before, await get_model_row(model_uuid)


async def seed_missing(entries: list[dict[str, Any]], already_seeded: set[str]) -> list[str]:
    """
    Inserts catalogue entries that were never offered before (`already_seeded` holds their
    "provider:model" keys). Returns the keys newly offered — inserted, or found to exist
    already — so the caller can record them. Default/fallback flags are applied only where
    the provider has none yet, so an admin's choice is never overridden.
    """
    offered: list[str] = []
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for entry in entries:
                key = f"{entry['provider']}:{entry['model_id']}"
                if key in already_seeded:
                    continue
                provider_db_id = await _provider_db_id(conn, entry["provider"])
                record = await conn.fetchrow(
                    """
                    INSERT INTO ai_models
                      (provider_id, model_id, display_name, status, context_window, input_modalities,
                       output_modalities, online_capable, offline_capable, config)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
                    ON CONFLICT (provider_id, model_id) DO NOTHING
                    RETURNING id
                    """,
                    provider_db_id,
                    entry["model_id"],
                    entry["display_name"],
                    entry["status"],
                    entry.get("context_window"),
                    entry.get("input_modalities") or ["text"],
                    entry.get("output_modalities") or ["text"],
                    entry.get("online_capable", True),
                    entry.get("offline_capable", False),
                    json.dumps(entry.get("config") or {}),
                )
                if record is None:
                    # Already present (created earlier by traffic): keep it exactly as it is, but it
                    # may still need to become the provider's default/fallback below.
                    record = await conn.fetchrow(
                        "SELECT id FROM ai_models WHERE provider_id = $1 AND model_id = $2",
                        provider_db_id,
                        entry["model_id"],
                    )
                if record is not None:
                    for flag, other, eligible in (
                        ("is_default", "is_fallback", DEFAULT_ELIGIBLE),
                        ("is_fallback", "is_default", FALLBACK_ELIGIBLE),
                    ):
                        if entry.get(flag):
                            # Only where the provider has none yet (an admin's choice is never
                            # overridden), and never onto a row that already holds the other slot —
                            # e.g. an admin made the catalogue's fallback model the default.
                            await conn.execute(
                                f"""
                                UPDATE ai_models SET {flag} = true
                                 WHERE id = $1 AND status = ANY($3::text[]) AND NOT {other}
                                   AND NOT EXISTS (SELECT 1 FROM ai_models WHERE provider_id = $2 AND {flag})
                                """,
                                record["id"],
                                provider_db_id,
                                list(eligible),
                            )
                offered.append(key)
    return offered
