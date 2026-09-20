"""
Governance engine tests — Passage 4 §3.5 (G05). These exercise
`ToolRegistry.execute()` against small hand-built tool definitions
rather than the 15 built-in tools, so each governance check (permission,
rate limit, timeout, schema) is isolated from any particular tool's own
behaviour or external dependencies.
"""

from __future__ import annotations

import asyncio

import pytest

from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolContext, ToolDefinition, ToolResult


def _echo_tool(**overrides) -> ToolDefinition:
    async def handler(input_data: dict, context) -> ToolResult:
        return ToolResult(output={"echo": input_data.get("value", "")})

    defaults = dict(
        name="echo",
        description="test tool",
        input_schema={
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        output_schema={
            "type": "object",
            "required": ["echo"],
            "properties": {"echo": {"type": "string"}},
        },
        handler=handler,
    )
    defaults.update(overrides)
    return ToolDefinition(**defaults)


@pytest.fixture
def registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(_echo_tool())
    return r


@pytest.mark.asyncio
async def test_successful_call_returns_ok_with_the_handler_output(registry):
    outcome = await registry.execute("echo", {"value": "hi"}, ToolContext(provider_id="mock"))
    assert outcome.status == "ok"
    assert outcome.output == {"echo": "hi"}
    assert outcome.error is None


@pytest.mark.asyncio
async def test_unknown_tool_returns_a_clear_error_not_an_exception(registry):
    outcome = await registry.execute("does_not_exist", {}, ToolContext(provider_id="mock"))
    assert outcome.status == "error"
    assert "Unknown tool" in outcome.error


@pytest.mark.asyncio
async def test_input_failing_schema_validation_never_reaches_the_handler():
    calls = []

    async def handler(input_data, context):
        calls.append(input_data)
        return ToolResult(output={"echo": "should not run"})

    registry = ToolRegistry()
    registry.register(
        _echo_tool(handler=handler, input_schema={"type": "object", "required": ["value"]})
    )
    outcome = await registry.execute("echo", {}, ToolContext(provider_id="mock"))
    assert outcome.status == "error"
    assert "schema validation" in outcome.error
    assert calls == []  # the handler must never have been invoked


@pytest.mark.asyncio
async def test_handler_violating_its_own_output_schema_is_an_error():
    async def bad_handler(input_data, context):
        return ToolResult(output={"wrong_key": "oops"})  # violates the echo schema

    registry = ToolRegistry()
    registry.register(_echo_tool(handler=bad_handler))
    outcome = await registry.execute("echo", {"value": "x"}, ToolContext(provider_id="mock"))
    assert outcome.status == "error"
    assert "violated its own output schema" in outcome.error


@pytest.mark.asyncio
async def test_provider_not_in_allowed_list_is_rejected():
    registry = ToolRegistry()
    registry.register(_echo_tool(allowed_providers=("grok",)))
    outcome = await registry.execute("echo", {"value": "x"}, ToolContext(provider_id="mock"))
    assert outcome.status == "error"
    assert "not permitted" in outcome.error


@pytest.mark.asyncio
async def test_allowed_provider_succeeds_when_the_list_is_restricted():
    registry = ToolRegistry()
    registry.register(_echo_tool(allowed_providers=("grok", "gemma")))
    outcome = await registry.execute("echo", {"value": "x"}, ToolContext(provider_id="grok"))
    assert outcome.status == "ok"


@pytest.mark.asyncio
async def test_empty_allowed_providers_means_every_provider_may_call_it():
    registry = ToolRegistry()
    registry.register(_echo_tool(allowed_providers=()))
    for provider_id in ("custom", "grok", "gemma", "mock"):
        outcome = await registry.execute("echo", {"value": "x"}, ToolContext(provider_id=provider_id))
        assert outcome.status == "ok"


@pytest.mark.asyncio
async def test_rate_limit_is_enforced():
    registry = ToolRegistry()
    registry.register(_echo_tool(rate_limit_per_minute=2))
    ctx = ToolContext(provider_id="mock")

    first = await registry.execute("echo", {"value": "1"}, ctx)
    second = await registry.execute("echo", {"value": "2"}, ctx)
    third = await registry.execute("echo", {"value": "3"}, ctx)

    assert first.status == "ok"
    assert second.status == "ok"
    assert third.status == "error"
    assert "Rate limit" in third.error


@pytest.mark.asyncio
async def test_timeout_is_enforced_and_reported_as_its_own_status():
    async def slow_handler(input_data, context):
        await asyncio.sleep(10)
        return ToolResult(output={"echo": "too slow"})

    registry = ToolRegistry()
    registry.register(_echo_tool(handler=slow_handler, timeout_seconds=0.05))
    outcome = await registry.execute("echo", {"value": "x"}, ToolContext(provider_id="mock"))
    assert outcome.status == "timeout"
    assert "timed out" in outcome.error


@pytest.mark.asyncio
async def test_handler_exception_is_caught_and_reported_not_raised():
    async def broken_handler(input_data, context):
        raise RuntimeError("boom")

    registry = ToolRegistry()
    registry.register(_echo_tool(handler=broken_handler))
    outcome = await registry.execute("echo", {"value": "x"}, ToolContext(provider_id="mock"))
    assert outcome.status == "error"
    assert "boom" in outcome.error


@pytest.mark.asyncio
async def test_tool_reporting_unavailable_still_returns_ok_status():
    """
    Unavailability (Post-MVP tools, missing data) is a legitimate content
    outcome, not an execution failure — the call itself succeeded, it
    just has nothing to report. Status stays "ok"; `available=False`
    carries the real information.
    """

    async def unavailable_handler(input_data, context):
        return ToolResult(output={"echo": ""}, available=False, unavailable_reason="no data yet")

    registry = ToolRegistry()
    registry.register(_echo_tool(handler=unavailable_handler))
    outcome = await registry.execute("echo", {"value": "x"}, ToolContext(provider_id="mock"))
    assert outcome.status == "ok"
    assert outcome.available is False


def test_registering_a_duplicate_name_is_rejected():
    registry = ToolRegistry()
    registry.register(_echo_tool())
    with pytest.raises(ValueError):
        registry.register(_echo_tool())