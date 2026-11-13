"""
`system_settings` — Passage 1 §8 "System memory: config, rules, thresholds —
versioned, admin-editable". Passage 4 §5.2: system_settings(id, key, value jsonb,
updated_by, updated_at), `key` UNIQUE.

"Versioned" is delivered by the audit trail rather than a column: every admin
write goes through `record_audit` with the previous and the new value, so the
history of a setting is the sequence of its audit rows.

Keys in use:
  circuit_breaker           admin-tuned breaker thresholds (Passage 4 §3.3 / G03)
  model_catalogue_seeded    "provider:model" keys already offered to ai_models, so an
                            admin's deletion of a seeded row is not undone on restart
"""

from __future__ import annotations

import json
from typing import Any

from .client import get_pool


async def get_setting(key: str) -> Any | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT value FROM system_settings WHERE key = $1", key)
    return json.loads(row["value"]) if row is not None else None


async def set_setting(key: str, value: Any, updated_by: str | None) -> Any | None:
    """Upserts `key` and returns the PREVIOUS value (None when it did not exist)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            previous = await conn.fetchrow("SELECT value FROM system_settings WHERE key = $1 FOR UPDATE", key)
            await conn.execute(
                """
                INSERT INTO system_settings (key, value, updated_by)
                VALUES ($1, $2::jsonb, $3)
                ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()
                """,
                key,
                json.dumps(value),
                updated_by,
            )
    return json.loads(previous["value"]) if previous is not None else None


async def delete_setting(key: str) -> bool:
    pool = await get_pool()
    return (await pool.execute("DELETE FROM system_settings WHERE key = $1", key)) == "DELETE 1"
