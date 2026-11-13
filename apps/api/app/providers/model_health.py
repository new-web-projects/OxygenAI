"""
Per-model health memory — the "health check" stage of Passage 1 §4.5's router:

    if not provider.health_ok():
        model = registry.get_fallback(provider) if provider.has_configured_fallback() else None

`health_ok()` is deliberately NOT a live probe on every request: a "ping" per analysis
would double the calls (and the token cost) of every hosted provider. Health is instead
remembered from what the system has already observed about a specific (provider, model):

* two consecutive failed calls to that model, within the TTL, or
* a failed admin probe (`POST /api/ai/{id}/test`, `GET /api/ai/{id}/health`) newer than
  its last success.

A success clears it; so does the TTL expiring (after which one real call is the next probe).
This is a *model*-level signal and is distinct from the circuit breaker, which judges the
*provider* as a whole and short-circuits it. Here the point is narrower: when the provider's
default model is the thing that is failing and a different model of the same provider is
configured as its fallback, use that one instead of failing the request.

In-process by design, like the circuit breaker (Passage 1 §3 puts shared state in Redis; that
is the Redis phase, not this one).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

TTL_SECONDS = 120.0
CONSECUTIVE_FAILURES_TO_FLAG = 2


@dataclass
class _ModelHealth:
    consecutive_failures: int = 0
    last_failure_at: float | None = None
    last_success_at: float | None = None
    probe_failed_at: float | None = None
    last_detail: str | None = None


class ModelHealthTracker:
    def __init__(self) -> None:
        self._state: dict[tuple[str, str], _ModelHealth] = {}

    def _get(self, provider_id: str, model_id: str) -> _ModelHealth:
        return self._state.setdefault((provider_id, model_id), _ModelHealth())

    def record_success(self, provider_id: str, model_id: str) -> None:
        state = self._get(provider_id, model_id)
        state.consecutive_failures = 0
        state.last_success_at = time.monotonic()
        state.probe_failed_at = None
        state.last_detail = None

    def record_failure(self, provider_id: str, model_id: str, detail: str) -> None:
        state = self._get(provider_id, model_id)
        state.consecutive_failures += 1
        state.last_failure_at = time.monotonic()
        state.last_detail = detail[:300]

    def record_probe(self, provider_id: str, model_id: str, ok: bool, detail: str | None = None) -> None:
        if ok:
            self.record_success(provider_id, model_id)
            return
        state = self._get(provider_id, model_id)
        state.probe_failed_at = time.monotonic()
        state.last_detail = (detail or "probe failed")[:300]

    def is_healthy(self, provider_id: str, model_id: str) -> bool:
        state = self._state.get((provider_id, model_id))
        if state is None:
            return True
        now = time.monotonic()
        if (
            state.probe_failed_at is not None
            and now - state.probe_failed_at < TTL_SECONDS
            and (state.last_success_at is None or state.last_success_at < state.probe_failed_at)
        ):
            return False
        if (
            state.consecutive_failures >= CONSECUTIVE_FAILURES_TO_FLAG
            and state.last_failure_at is not None
            and now - state.last_failure_at < TTL_SECONDS
        ):
            return False
        return True

    def detail(self, provider_id: str, model_id: str) -> str | None:
        state = self._state.get((provider_id, model_id))
        return state.last_detail if state else None

    def reset(self, provider_id: str | None = None, model_id: str | None = None) -> None:
        if provider_id is None:
            self._state.clear()
            return
        for key in [k for k in self._state if k[0] == provider_id and (model_id is None or k[1] == model_id)]:
            del self._state[key]


_tracker: ModelHealthTracker | None = None


def get_model_health() -> ModelHealthTracker:
    global _tracker
    if _tracker is None:
        _tracker = ModelHealthTracker()
    return _tracker


def reset_model_health_for_tests() -> None:
    global _tracker
    _tracker = None
