"""
Provider circuit breaker — Passage 1 §4.5, with the defaults and
rationale Passage 4 §3.3 (G03) recovers:

  "After N consecutive failures within a rolling window (recommended
   default: 5 failures / 60 seconds, admin-configurable), the router
   trips a breaker for that provider — it short-circuits further calls
   to a controlled error/fallback for a cool-down period instead of
   retrying a service that's clearly down, which protects both latency
   and the provider's own rate limits during an outage."

Passage 4 is equally explicit that this is one of *three* distinct
mechanisms that are "required together, not as substitutes for one
another":

  1. per-request retry/timeout  -> providers/transport.py
  2. task-level isolation       -> comparison.py's asyncio.gather
  3. the circuit breaker        -> this module

Before this pass only (2) existed. (1) and (3) are new here.

State machine: CLOSED -> (threshold failures inside the window) -> OPEN
-> (cooldown elapses) -> HALF_OPEN -> (one trial call) -> CLOSED on
success, OPEN again on failure. HALF_OPEN is what stops a recovered
provider from staying locked out until something manually resets it.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from ..config import CircuitBreakerSettings, get_settings
from ..observability import get_logger

logger = get_logger("circuit_breaker")

BreakerState = Literal["closed", "open", "half_open"]


@dataclass
class BreakerSnapshot:
    provider_id: str
    state: BreakerState
    recent_failures: int
    failure_threshold: int
    window_seconds: float
    cooldown_seconds: float
    opened_at: float | None
    retry_after_seconds: float | None
    last_error: str | None
    total_successes: int
    total_failures: int
    total_short_circuits: int


@dataclass
class _ProviderBreaker:
    provider_id: str
    settings: CircuitBreakerSettings
    failures: deque[float] = field(default_factory=deque)
    state: BreakerState = "closed"
    opened_at: float | None = None
    last_error: str | None = None
    total_successes: int = 0
    total_failures: int = 0
    total_short_circuits: int = 0
    half_open_in_flight: bool = False

    def _prune(self, now: float) -> None:
        cutoff = now - self.settings.window_seconds
        while self.failures and self.failures[0] < cutoff:
            self.failures.popleft()

    def is_open(self, now: float) -> bool:
        if self.state == "closed":
            return False
        if self.state == "half_open":
            # One trial call is allowed through; everything else while
            # that trial is in flight is still short-circuited.
            return self.half_open_in_flight
        assert self.opened_at is not None
        if now - self.opened_at >= self.settings.cooldown_seconds:
            self.state = "half_open"
            self.half_open_in_flight = False
            logger.info(
                "circuit breaker entering half-open", extra={"provider": self.provider_id}
            )
            return False
        return True

    def retry_after(self, now: float) -> float | None:
        if self.state != "open" or self.opened_at is None:
            return None
        remaining = self.settings.cooldown_seconds - (now - self.opened_at)
        return max(0.0, round(remaining, 2))

    def record_success(self, now: float) -> None:
        self.total_successes += 1
        self.failures.clear()
        self.half_open_in_flight = False
        if self.state != "closed":
            logger.info("circuit breaker closed", extra={"provider": self.provider_id})
        self.state = "closed"
        self.opened_at = None
        self.last_error = None

    def record_failure(self, now: float, error: str) -> None:
        self.total_failures += 1
        self.last_error = error
        self.half_open_in_flight = False

        if self.state == "half_open":
            # The trial call failed — straight back to open, full cooldown.
            self.state = "open"
            self.opened_at = now
            logger.warning(
                "circuit breaker re-opened after failed trial",
                extra={"provider": self.provider_id, "error": error},
            )
            return

        self.failures.append(now)
        self._prune(now)
        if len(self.failures) >= self.settings.failure_threshold:
            self.state = "open"
            self.opened_at = now
            logger.warning(
                "circuit breaker opened",
                extra={
                    "provider": self.provider_id,
                    "failures": len(self.failures),
                    "windowSeconds": self.settings.window_seconds,
                    "cooldownSeconds": self.settings.cooldown_seconds,
                },
            )

    def snapshot(self, now: float) -> BreakerSnapshot:
        self._prune(now)
        return BreakerSnapshot(
            provider_id=self.provider_id,
            state=self.state,
            recent_failures=len(self.failures),
            failure_threshold=self.settings.failure_threshold,
            window_seconds=self.settings.window_seconds,
            cooldown_seconds=self.settings.cooldown_seconds,
            opened_at=self.opened_at,
            retry_after_seconds=self.retry_after(now),
            last_error=self.last_error,
            total_successes=self.total_successes,
            total_failures=self.total_failures,
            total_short_circuits=self.total_short_circuits,
        )


class CircuitBreakerOpen(Exception):
    """Raised instead of calling a provider whose breaker is open."""

    def __init__(self, provider_id: str, retry_after: float | None, last_error: str | None):
        self.provider_id = provider_id
        self.retry_after = retry_after
        self.last_error = last_error
        detail = f" Last error: {last_error}" if last_error else ""
        wait = f" Retry in ~{retry_after:.0f}s." if retry_after is not None else ""
        super().__init__(
            f"Circuit breaker is open for '{provider_id}' — calls are short-circuited "
            f"rather than retried against a provider that is failing.{wait}{detail}"
        )


class CircuitBreakerRegistry:
    """
    Per-provider breakers. In-process by design for now: with a single
    API instance this is correct and adds no infrastructure. Passage 1
    §3 lists Redis for shared state, which is what a multi-instance
    deployment would need — flagged here rather than silently assumed
    to work across replicas.
    """

    def __init__(self, settings: CircuitBreakerSettings | None = None) -> None:
        self._settings = settings or get_settings().circuit_breaker
        self._breakers: dict[str, _ProviderBreaker] = {}
        self._lock = asyncio.Lock()

    def _get(self, provider_id: str) -> _ProviderBreaker:
        breaker = self._breakers.get(provider_id)
        if breaker is None:
            breaker = _ProviderBreaker(provider_id=provider_id, settings=self._settings)
            self._breakers[provider_id] = breaker
        return breaker

    async def ensure_closed(self, provider_id: str) -> None:
        """Raise CircuitBreakerOpen if this provider is currently short-circuited."""
        now = time.monotonic()
        async with self._lock:
            breaker = self._get(provider_id)
            if breaker.is_open(now):
                breaker.total_short_circuits += 1
                raise CircuitBreakerOpen(
                    provider_id, breaker.retry_after(now), breaker.last_error
                )
            if breaker.state == "half_open":
                breaker.half_open_in_flight = True

    async def record_success(self, provider_id: str) -> None:
        async with self._lock:
            self._get(provider_id).record_success(time.monotonic())

    async def record_failure(self, provider_id: str, error: str) -> None:
        async with self._lock:
            self._get(provider_id).record_failure(time.monotonic(), error)

    async def snapshot(self, provider_id: str) -> BreakerSnapshot:
        async with self._lock:
            return self._get(provider_id).snapshot(time.monotonic())

    async def snapshot_all(self) -> list[BreakerSnapshot]:
        async with self._lock:
            now = time.monotonic()
            return [b.snapshot(now) for b in self._breakers.values()]

    async def reset(self, provider_id: str | None = None) -> None:
        """Admin action: clear a tripped breaker without a restart."""
        async with self._lock:
            if provider_id is None:
                self._breakers.clear()
            else:
                self._breakers.pop(provider_id, None)


_registry: CircuitBreakerRegistry | None = None


def get_circuit_breakers() -> CircuitBreakerRegistry:
    global _registry
    if _registry is None:
        _registry = CircuitBreakerRegistry()
    return _registry


def reset_circuit_breakers_for_tests() -> None:
    global _registry
    _registry = None