"""
The governed call path every tool goes through — Passage 1 §4.8 /
Passage 4 §3.5 (G05), made real rather than described.

`execute()` is the one and only way a tool is ever invoked. There is no
second path that skips governance — a provider (or, in this pass, the
mock provider directly) never calls a tool's handler function itself;
it always goes through `ToolRegistry.execute()`, which is what makes
"none of the three ever executes arbitrary code" an enforced property
rather than a convention someone could accidentally bypass.

Order of checks, each one able to short-circuit the rest: tool exists →
provider is allowed to call it → rate limit → input validates against
the declared JSON Schema → timeout-bounded execution → output validates
against the declared JSON Schema. Every outcome — success, and every
kind of failure — is logged to `tool_calls` (best-effort; a logging
failure never masks or replaces the real result).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import jsonschema

from ..db.client import is_db_configured
from ..db.tools import get_tool_db_id, log_tool_call, seed_all_tools
from ..observability import get_logger
from .rate_limiter import ToolRateLimiter
from .schemas import ToolContext, ToolDefinition, ToolExecutionOutcome

logger = get_logger("tools.registry")


class ToolNotFoundError(Exception):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._db_ids: dict[str, str] = {}
        self._rate_limiter = ToolRateLimiter()
        self._seeded = False

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered — names must be unique")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def all(self) -> list[ToolDefinition]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    async def seed_database(self) -> None:
        """
        Upserts every registered tool into `tool_registry`. Called once
        at startup (`main.py`'s lifespan handler); safe to call again —
        idempotent by name. A missing/unreachable database at startup is
        not fatal: `execute()` falls back to a lazy per-call lookup, and
        logging is skipped entirely (not raised) when the database isn't
        configured, matching every other best-effort DB write in this
        codebase.
        """
        if not is_db_configured():
            logger.info("database not configured; tool_registry seeding skipped")
            return
        try:
            self._db_ids = await seed_all_tools(self.all())
            self._seeded = True
            logger.info("tool registry seeded", extra={"count": len(self._db_ids)})
        except Exception as err:  # noqa: BLE001 — seeding failure must not block startup
            logger.warning("tool registry seeding failed", extra={"error": str(err)})

    async def _resolve_db_id(self, name: str) -> str | None:
        if name in self._db_ids:
            return self._db_ids[name]
        if not is_db_configured():
            return None
        db_id = await get_tool_db_id(name)
        if db_id:
            self._db_ids[name] = db_id
        return db_id

    async def _log(
        self, tool_name: str, outcome: ToolExecutionOutcome, input_data: dict, context: ToolContext
    ) -> None:
        if not is_db_configured():
            return
        try:
            db_id = await self._resolve_db_id(tool_name)
            if db_id is None:
                return
            await log_tool_call(db_id, outcome, input_data, context.conversation_id)
        except Exception as err:  # noqa: BLE001 — logging must never mask the real outcome
            logger.warning(
                "failed to log tool call", extra={"tool": tool_name, "error": str(err)}
            )

    async def execute(
        self, tool_name: str, input_data: dict[str, Any], context: ToolContext
    ) -> ToolExecutionOutcome:
        started = time.perf_counter()
        tool = self.get(tool_name)

        if tool is None:
            outcome = ToolExecutionOutcome(
                tool_name=tool_name,
                status="error",
                output=None,
                error=f"Unknown tool: '{tool_name}'",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            return outcome  # nothing to log against — there is no tool_registry row for a name that doesn't exist

        if not tool.is_allowed_for(context.provider_id):
            outcome = ToolExecutionOutcome(
                tool_name=tool_name,
                status="error",
                output=None,
                error=(
                    f"Provider '{context.provider_id}' is not permitted to call '{tool_name}' "
                    f"(allowed: {', '.join(tool.allowed_providers)})"
                ),
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            await self._log(tool_name, outcome, input_data, context)
            return outcome

        allowed, retry_after = self._rate_limiter.try_consume(
            tool_name, tool.rate_limit_per_minute
        )
        if not allowed:
            outcome = ToolExecutionOutcome(
                tool_name=tool_name,
                status="error",
                output=None,
                error=(
                    f"Rate limit exceeded for '{tool_name}' "
                    f"({tool.rate_limit_per_minute}/min) — retry in ~{retry_after:.0f}s"
                ),
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            await self._log(tool_name, outcome, input_data, context)
            return outcome

        try:
            jsonschema.validate(input_data, tool.input_schema)
        except jsonschema.ValidationError as err:
            outcome = ToolExecutionOutcome(
                tool_name=tool_name,
                status="error",
                output=None,
                error=f"Input failed schema validation: {err.message}",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            await self._log(tool_name, outcome, input_data, context)
            return outcome

        try:
            result = await asyncio.wait_for(
                tool.handler(input_data, context), timeout=tool.timeout_seconds
            )
        except asyncio.TimeoutError:
            outcome = ToolExecutionOutcome(
                tool_name=tool_name,
                status="timeout",
                output=None,
                error=f"Tool timed out after {tool.timeout_seconds}s",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            await self._log(tool_name, outcome, input_data, context)
            return outcome
        except Exception as err:  # noqa: BLE001 — a handler's own bug must not crash the caller
            outcome = ToolExecutionOutcome(
                tool_name=tool_name,
                status="error",
                output=None,
                error=f"{type(err).__name__}: {err}",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            await self._log(tool_name, outcome, input_data, context)
            return outcome

        try:
            jsonschema.validate(result.output, tool.output_schema)
        except jsonschema.ValidationError as err:
            # A handler returning something that violates its own
            # declared contract is the handler's bug, not the caller's —
            # surfaced as an error rather than silently passed through,
            # which would make the output schema decorative.
            outcome = ToolExecutionOutcome(
                tool_name=tool_name,
                status="error",
                output=None,
                error=f"Tool '{tool_name}' violated its own output schema: {err.message}",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            await self._log(tool_name, outcome, input_data, context)
            return outcome

        outcome = ToolExecutionOutcome(
            tool_name=tool_name,
            status="ok",
            output=result.output,
            error=None,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            available=result.available,
        )
        await self._log(tool_name, outcome, input_data, context)
        return outcome

    def reset_for_tests(self) -> None:
        self._rate_limiter.reset()


_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        from .builtin import ALL_TOOLS

        for tool in ALL_TOOLS:
            _registry.register(tool)
    return _registry


def reset_tool_registry_for_tests() -> None:
    global _registry
    _registry = None