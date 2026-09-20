"""
GET /api/ai/tools, POST /api/ai/tools/{name}/invoke.

Not a named endpoint in Passage 1 §10 or Passage 4's G13 category list
(there's no "Tools" category among the 19 named — Auth, Users, AI, Grok,
Custom AI, AI Provider Routing, AI Model Management, Market Data,
Indicators, Trading Signals, Backtesting, Paper Trading, Knowledge,
Memory, Voice, Admin, Monitoring, Errors, Usage). Added because a
governed registry with no way to inspect what's registered or exercise
it manually is hard to operate or debug — the same reasoning that
justifies `GET /api/ai/providers` existing at all. Admin-gated, matching
every other inspection/administrative surface built so far.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..errors import NotFoundError
from ..security.dependencies import require_role
from ..tools.registry import get_tool_registry
from ..tools.schemas import ToolContext

router = APIRouter(
    prefix="/api/ai/tools", tags=["tools"], dependencies=[Depends(require_role("admin"))]
)


@router.get("")
async def list_tools() -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "kind": t.kind,
            "inputSchema": t.input_schema,
            "outputSchema": t.output_schema,
            "allowedProviders": list(t.allowed_providers) or ["all"],
            "timeoutSeconds": t.timeout_seconds,
            "rateLimitPerMinute": t.rate_limit_per_minute,
            "availabilityNote": t.availability_note or None,
        }
        for t in get_tool_registry().all()
    ]


@router.post("/{name}/invoke")
async def invoke_tool(name: str, input_data: dict) -> dict:
    """
    Manual invocation for admin debugging/verification — goes through
    the exact same `execute()` governance path a provider would use,
    tagged with provider_id="custom" so a call against a
    provider-restricted tool is checked the same way a real one would
    be.
    """
    registry = get_tool_registry()
    if registry.get(name) is None:
        raise NotFoundError(f"Unknown tool: '{name}'")
    outcome = await registry.execute(name, input_data, ToolContext(provider_id="custom"))
    return {
        "tool": outcome.tool_name,
        "status": outcome.status,
        "output": outcome.output,
        "error": outcome.error,
        "latencyMs": outcome.latency_ms,
        "available": outcome.available,
    }