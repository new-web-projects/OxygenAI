"""
Per-tool rate limiting. The same token-bucket algorithm as
`app/rate_limit.py`, kept as a separate small implementation rather than
importing that module's private class: an HTTP request and a tool call
are different call shapes (one is keyed by client IP off a Starlette
`Request`, the other by tool name with no HTTP object involved at all),
and forcing one class to serve both would couple two things that should
stay independently editable.

In-process, matching the same documented limitation as the circuit
breaker and the HTTP rate limiter: correct for a single API instance,
would need Redis-backed state to be correct across replicas.
"""

from __future__ import annotations

import time


class ToolRateLimiter:
    def __init__(self) -> None:
        self._tokens: dict[str, float] = {}
        self._last_refill: dict[str, float] = {}

    def try_consume(self, tool_name: str, per_minute: int) -> tuple[bool, float]:
        if per_minute <= 0:
            return True, 0.0
        now = time.monotonic()
        capacity = float(per_minute)
        refill_rate = per_minute / 60.0

        tokens = self._tokens.get(tool_name, capacity)
        last = self._last_refill.get(tool_name, now)
        tokens = min(capacity, tokens + (now - last) * refill_rate)

        if tokens >= 1.0:
            self._tokens[tool_name] = tokens - 1.0
            self._last_refill[tool_name] = now
            return True, 0.0

        self._tokens[tool_name] = tokens
        self._last_refill[tool_name] = now
        wait = (1.0 - tokens) / refill_rate if refill_rate > 0 else 60.0
        return False, wait

    def reset(self) -> None:
        """Test-only: clear all bucket state."""
        self._tokens.clear()
        self._last_refill.clear()