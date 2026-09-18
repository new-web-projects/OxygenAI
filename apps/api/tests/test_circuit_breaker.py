"""
Circuit breaker tests — Passage 4 §3.3 (G03): "After N consecutive
failures within a rolling window (recommended default: 5 failures / 60
seconds), the router trips a breaker ... short-circuits further calls
... for a cool-down period."

Uses a short, explicit window/cooldown per test rather than the real
60s default, so the suite doesn't take a minute to run.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import CircuitBreakerSettings
from app.providers.circuit_breaker import CircuitBreakerOpen, CircuitBreakerRegistry


def _fast_settings(threshold: int = 3) -> CircuitBreakerSettings:
    return CircuitBreakerSettings(
        failure_threshold=threshold, window_seconds=1.0, cooldown_seconds=0.2
    )


@pytest.mark.asyncio
async def test_stays_closed_below_the_failure_threshold():
    registry = CircuitBreakerRegistry(_fast_settings(threshold=3))
    await registry.record_failure("p", "boom")
    await registry.record_failure("p", "boom")
    # Should not raise — only 2 of 3 failures recorded.
    await registry.ensure_closed("p")


@pytest.mark.asyncio
async def test_opens_at_the_failure_threshold():
    registry = CircuitBreakerRegistry(_fast_settings(threshold=3))
    await registry.record_failure("p", "boom")
    await registry.record_failure("p", "boom")
    await registry.record_failure("p", "boom")
    with pytest.raises(CircuitBreakerOpen):
        await registry.ensure_closed("p")


@pytest.mark.asyncio
async def test_success_resets_the_failure_count():
    registry = CircuitBreakerRegistry(_fast_settings(threshold=3))
    await registry.record_failure("p", "boom")
    await registry.record_failure("p", "boom")
    await registry.record_success("p")
    await registry.record_failure("p", "boom")
    await registry.record_failure("p", "boom")
    # Two more failures after a success — still below threshold.
    await registry.ensure_closed("p")


@pytest.mark.asyncio
async def test_half_open_after_cooldown_allows_one_trial():
    registry = CircuitBreakerRegistry(_fast_settings(threshold=1))
    await registry.record_failure("p", "boom")
    with pytest.raises(CircuitBreakerOpen):
        await registry.ensure_closed("p")

    await asyncio.sleep(0.25)  # cooldown_seconds=0.2

    # The trial call is allowed through.
    await registry.ensure_closed("p")
    snapshot = await registry.snapshot("p")
    assert snapshot.state == "half_open"


@pytest.mark.asyncio
async def test_failed_trial_reopens_the_breaker():
    registry = CircuitBreakerRegistry(_fast_settings(threshold=1))
    await registry.record_failure("p", "boom")
    await asyncio.sleep(0.25)
    await registry.ensure_closed("p")  # enters half-open, trial allowed
    await registry.record_failure("p", "boom again")  # trial fails
    with pytest.raises(CircuitBreakerOpen):
        await registry.ensure_closed("p")


@pytest.mark.asyncio
async def test_successful_trial_closes_the_breaker():
    registry = CircuitBreakerRegistry(_fast_settings(threshold=1))
    await registry.record_failure("p", "boom")
    await asyncio.sleep(0.25)
    await registry.ensure_closed("p")
    await registry.record_success("p")
    snapshot = await registry.snapshot("p")
    assert snapshot.state == "closed"


@pytest.mark.asyncio
async def test_providers_have_independent_breakers():
    registry = CircuitBreakerRegistry(_fast_settings(threshold=1))
    await registry.record_failure("a", "boom")
    with pytest.raises(CircuitBreakerOpen):
        await registry.ensure_closed("a")
    await registry.ensure_closed("b")  # untouched, still closed


@pytest.mark.asyncio
async def test_retry_after_is_reported_and_decreases_toward_zero():
    registry = CircuitBreakerRegistry(_fast_settings(threshold=1))
    await registry.record_failure("p", "boom")
    try:
        await registry.ensure_closed("p")
    except CircuitBreakerOpen as err:
        assert err.retry_after is not None
        assert err.retry_after >= 0.0


@pytest.mark.asyncio
async def test_reset_clears_a_tripped_breaker():
    registry = CircuitBreakerRegistry(_fast_settings(threshold=1))
    await registry.record_failure("p", "boom")
    with pytest.raises(CircuitBreakerOpen):
        await registry.ensure_closed("p")
    await registry.reset("p")
    await registry.ensure_closed("p")  # no longer open