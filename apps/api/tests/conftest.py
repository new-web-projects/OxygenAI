"""
Shared pytest fixtures.

The circuit breaker registry (`app.providers.circuit_breaker`) is a
process-level singleton by design — Passage 1 §4.5's router state is
meant to persist across requests within one running API instance. Left
alone across a pytest session, though, that means failures recorded by
one test file accumulate toward another file's threshold, making the
suite's pass/fail depend on file execution order. This fixture resets it
before every test, so each test starts from a clean CLOSED breaker
regardless of what ran before it.
"""

from __future__ import annotations

import pytest

from app.providers.circuit_breaker import reset_circuit_breakers_for_tests


@pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    reset_circuit_breakers_for_tests()
    yield
    reset_circuit_breakers_for_tests()