"""
`tool_registry` and `tool_calls` — both existed in the schema before
this pass (Passage 4 §5.2/G12) and were confirmed unused by any code
path in the original audit. This is the first application code to use
either.

`upsert_tool_definition` is idempotent by `name` (the table's existing
UNIQUE constraint) — safe to call on every startup as the Python-side
tool list evolves, without ever duplicating a row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..tools.schemas import ToolDefinition, ToolExecutionOutcome
from .client import get_pool


@dataclass(frozen=True)
class RegisteredTool:
    db_id: str
    name: str


async def upsert_tool_definition(tool: ToolDefinition) -> RegisteredTool:
    pool = await get_pool()
    permissions = {
        "allowedProviders": list(tool.allowed_providers),
        "kind": tool.kind,
    }
    row = await pool.fetchrow(
        """
        INSERT INTO tool_registry (name, description, input_schema, output_schema, permissions, timeout_ms, rate_limit)
        VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb, $6, $7)
        ON CONFLICT (name) DO UPDATE SET
            description = EXCLUDED.description,
            input_schema = EXCLUDED.input_schema,
            output_schema = EXCLUDED.output_schema,
            permissions = EXCLUDED.permissions,
            timeout_ms = EXCLUDED.timeout_ms,
            rate_limit = EXCLUDED.rate_limit
        RETURNING id, name
        """,
        tool.name,
        tool.description,
        json.dumps(tool.input_schema),
        json.dumps(tool.output_schema),
        json.dumps(permissions),
        int(tool.timeout_seconds * 1000),
        tool.rate_limit_per_minute,
    )
    assert row is not None
    return RegisteredTool(db_id=str(row["id"]), name=row["name"])


async def get_tool_db_id(name: str) -> str | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT id FROM tool_registry WHERE name = $1", name)
    return str(row["id"]) if row else None


async def log_tool_call(
    tool_db_id: str,
    outcome: ToolExecutionOutcome,
    input_data: dict,
    conversation_id: str | None,
) -> None:
    """
    Best-effort — a logging failure must never take down the tool call
    it's trying to record. Callers wrap this the same way
    `routers/analyze.py` wraps `save_signal`: log a warning, don't raise.
    """
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO tool_calls (conversation_id, tool_id, input, output, status, error, latency_ms)
        VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7)
        """,
        conversation_id,
        tool_db_id,
        json.dumps(input_data),
        json.dumps(outcome.output) if outcome.output is not None else None,
        outcome.status,
        outcome.error,
        round(outcome.latency_ms),
    )


async def seed_all_tools(tools: list[ToolDefinition]) -> dict[str, str]:
    """
    Upserts every tool definition and returns {name: db_id}. Called once
    at startup (see `main.py`) and again lazily by `registry.execute()`
    if a tool's db id isn't cached yet — covers the case where the
    database wasn't reachable at startup but becomes available later.
    """
    result: dict[str, str] = {}
    for tool in tools:
        registered = await upsert_tool_definition(tool)
        result[registered.name] = registered.db_id
    return result