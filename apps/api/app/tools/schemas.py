"""
The data model behind the 15-tool registry — Passage 1 §4.8, recovered
in full by Passage 4 §3.5 (G05):

  "Every tool is registered with: name, description, input schema,
   output schema, permissions (which roles/modes may call it), timeout,
   rate limit, and structured logging of every call ... Every provider
   selects from this registry — none of the three (Custom AI, Grok, or
   Gemma) ever executes arbitrary code, and none ever receives a raw
   code-execution or shell tool. Data-fetching tools ... are read-only
   by construction; anything that could mutate state ... is scoped
   strictly to the requesting user's own data."

Every field G05 names is a real field here, not a comment describing
intent: `input_schema`/`output_schema` are genuine JSON Schema dicts,
validated with the `jsonschema` library (not hand-rolled key-presence
checks); `allowed_providers` is enforced (§4.7's "modes" vocabulary —
CUSTOM|GROK|GEMMA|MULTI — maps directly onto which provider id may call
a tool); `timeout_seconds` and `rate_limit_per_minute` are both actually
enforced by `registry.py`, not merely stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

# Passage 1 §2's three real providers, plus mock — the same set
# `providers/registry.py` already uses. A tool's `allowed_providers`
# lists which of these may invoke it; empty means "all".
ToolProviderId = Literal["custom", "grok", "gemma", "mock"]

ToolStatus = Literal["ok", "error", "timeout"]

# Read-only tools can never mutate state by construction (G05's own
# distinction); mutating tools must additionally be scoped to the
# acting user (`ToolContext.acting_user_id`), which each mutating
# tool's handler is responsible for checking against its own data.
ToolKind = Literal["read_only", "mutating"]


@dataclass(frozen=True)
class ToolContext:
    """
    What a tool handler is allowed to know about the call site — never
    the raw provider object, the database pool, or anything else that
    would let a "tool" become a side channel to arbitrary code. A
    handler receives only this plus its own validated input.
    """

    provider_id: ToolProviderId
    conversation_id: str | None = None
    acting_user_id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """A handler's return value before governance wraps it for logging."""

    output: dict[str, Any]
    # A handler sets this when it has real work to report but no real
    # data source yet (Post-MVP tools, vendor-less tools) — this is the
    # tool-level version of "null field, not a fabricated number":
    # reporting unavailability honestly rather than inventing a result.
    available: bool = True
    unavailable_reason: str | None = None


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: ToolHandler
    kind: ToolKind = "read_only"
    # Empty tuple = every provider may call it (the common case for
    # read-only data tools). Non-empty restricts to that set.
    allowed_providers: tuple[ToolProviderId, ...] = ()
    timeout_seconds: float = 10.0
    rate_limit_per_minute: int = 60
    # Named explicitly in Passage 4's own recovered table so a
    # Post-MVP/vendor-dependent tool's status is visible in the
    # registry listing, not just discoverable by calling it and getting
    # a failure.
    availability_note: str = ""

    def is_allowed_for(self, provider_id: ToolProviderId) -> bool:
        return not self.allowed_providers or provider_id in self.allowed_providers


@dataclass(frozen=True)
class ToolExecutionOutcome:
    """What `registry.execute()` returns — the full governance envelope, not just the raw result."""

    tool_name: str
    status: ToolStatus
    output: dict[str, Any] | None
    error: str | None
    latency_ms: float
    available: bool = True