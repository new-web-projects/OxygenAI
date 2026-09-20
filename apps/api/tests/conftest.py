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

The database connection pool (`app.db.client._pool`) has the same
process-level-singleton shape and the same test-suite problem, but for a
different underlying reason: an asyncpg pool is bound to the event loop
it was created in, and pytest.ini's `asyncio_default_fixture_loop_scope
= function` gives every test its own event loop. A pool created by test
A is unusable by the time test B runs against a fresh loop, and raises
"Event loop is closed" from deep inside asyncpg — not from anything
wrong in the test itself. Several test files originally worked around
this with a local `pool` fixture that reset `_pool` to None; moved here
as an autouse fixture so any test that touches the database gets a
correctly-scoped pool automatically, including ones (like a handful of
tool-registry tests) that call database-backed code indirectly without
necessarily expecting to need this.
"""

from __future__ import annotations

import pytest

from app.providers.circuit_breaker import reset_circuit_breakers_for_tests


@pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    reset_circuit_breakers_for_tests()
    yield
    reset_circuit_breakers_for_tests()


@pytest.fixture(autouse=True)
async def _reset_db_pool():
    import app.db.client as db_client

    db_client._pool = None
    yield
    db_client._pool = None