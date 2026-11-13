"""
`ai_provider_settings` — Passage 4 §5.2: ai_provider_settings(id, provider_id,
api_key_encrypted, timeout_ms, retry_count, base_url, region, updated_by, updated_at).

One row per provider (migration 0007). Every column except the key is an optional
override: NULL means "no override — use the environment / transport default". The API
key column holds ciphertext only (`security/secret_store.py`); this module never sees or
returns plaintext, and nothing here logs a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from .client import get_pool
from .signals import get_or_seed_provider

# Sentinel for "this field was not part of the update" (None means "clear it").
UNSET: Any = object()

_FIELDS = ("api_key_encrypted", "timeout_ms", "retry_count", "base_url", "region")


@dataclass
class ProviderSettingsRow:
    provider: str
    api_key_encrypted: str | None
    timeout_ms: int | None
    retry_count: int | None
    base_url: str | None
    region: str | None
    updated_by: str | None
    updated_at: datetime | None


_SELECT = """
SELECT p.name AS provider, s.api_key_encrypted, s.timeout_ms, s.retry_count, s.base_url,
       s.region, s.updated_by, s.updated_at
  FROM ai_provider_settings s JOIN ai_providers p ON p.id = s.provider_id
"""


def _row(record: asyncpg.Record) -> ProviderSettingsRow:
    return ProviderSettingsRow(
        provider=record["provider"],
        api_key_encrypted=record["api_key_encrypted"],
        timeout_ms=record["timeout_ms"],
        retry_count=record["retry_count"],
        base_url=record["base_url"],
        region=record["region"],
        updated_by=str(record["updated_by"]) if record["updated_by"] else None,
        updated_at=record["updated_at"],
    )


async def get_provider_settings(provider: str) -> ProviderSettingsRow | None:
    pool = await get_pool()
    record = await pool.fetchrow(_SELECT + " WHERE p.name = $1", provider)
    return _row(record) if record is not None else None


async def list_provider_settings() -> list[ProviderSettingsRow]:
    pool = await get_pool()
    return [_row(r) for r in await pool.fetch(_SELECT + " ORDER BY p.name")]


async def upsert_provider_settings(
    provider: str, updated_by: str | None, **fields: Any
) -> tuple[ProviderSettingsRow | None, ProviderSettingsRow]:
    """
    Applies only the fields passed (others untouched); passing None CLEARS a field.
    Returns (before, after).
    """
    unknown = set(fields) - set(_FIELDS)
    if unknown:
        raise ValueError(f"unsupported fields: {sorted(unknown)}")
    provider_db_id = await get_or_seed_provider(provider)
    before = await get_provider_settings(provider)

    columns = list(fields)
    insert_cols = ["provider_id", "updated_by", *columns]
    placeholders = ", ".join(f"${i}" for i in range(1, len(insert_cols) + 1))
    updates = ", ".join([f"{c} = EXCLUDED.{c}" for c in columns] + ["updated_by = EXCLUDED.updated_by", "updated_at = now()"])
    pool = await get_pool()
    await pool.execute(
        f"INSERT INTO ai_provider_settings ({', '.join(insert_cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (provider_id) DO UPDATE SET {updates}",
        provider_db_id,
        updated_by,
        *[fields[c] for c in columns],
    )
    after = await get_provider_settings(provider)
    assert after is not None
    return before, after


async def delete_provider_settings(provider: str) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM ai_provider_settings WHERE provider_id = (SELECT id FROM ai_providers WHERE name = $1)",
        provider,
    )
    return result == "DELETE 1"


async def list_provider_flags() -> dict[str, dict[str, Any]]:
    """`ai_providers.enabled` / `priority` for every provider the database knows."""
    pool = await get_pool()
    return {
        r["name"]: {"enabled": r["enabled"], "priority": r["priority"]}
        for r in await pool.fetch("SELECT name, enabled, priority FROM ai_providers")
    }


async def update_provider_flags(
    provider: str, *, enabled: bool | None = None, priority: int | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    provider_db_id = await get_or_seed_provider(provider)
    pool = await get_pool()
    before = dict(await pool.fetchrow("SELECT enabled, priority FROM ai_providers WHERE id = $1", provider_db_id))
    after = dict(
        await pool.fetchrow(
            """
            UPDATE ai_providers
               SET enabled = COALESCE($2, enabled), priority = COALESCE($3, priority), updated_at = now()
             WHERE id = $1 RETURNING enabled, priority
            """,
            provider_db_id,
            enabled,
            priority,
        )
    )
    return before, after
