"""
Database integration tests — audit item D7.1, flagged repeatedly as the
project's most significant structural verification gap: "every DB claim
in this project's history has been verified manually, live, during a
session, never captured as a repeatable test."

These tests require a real, reachable Postgres with the three
migrations applied (DATABASE_URL set) and are skipped, not failed, when
that isn't available — a missing test database is an environment fact,
not a code defect. Run them with:

    export DATABASE_URL=postgresql://user:pass@host:5432/dbname
    pytest tests/test_integration_db.py -v

Every row this file creates is prefixed `ZZTEST_` so it's identifiable
and safe to clean up, and each test cleans up its own rows in a
`finally` block rather than depending on test order or a shared
fixture's teardown running.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL is not set — DB integration tests require a live Postgres instance",
)


@pytest.fixture
async def pool():
    """
    Resets the module-level connection-pool singleton before and after
    each test. Left alone, the singleton (correct for a long-running
    server process, one event loop for its whole lifetime) is bound to
    whichever event loop created it — but pytest.ini's
    `asyncio_default_fixture_loop_scope = function` gives every test its
    own event loop, so a pool created in test A's loop is unusable by
    the time test B runs against a new one, and raises "Event loop is
    closed" from deep inside asyncpg. Forcing a fresh pool per test
    avoids the mismatch without changing the app's own production
    behaviour or the global pytest event-loop scope.
    """
    import app.db.client as db_client

    db_client._pool = None
    from app.db.client import get_pool

    created = await get_pool()
    yield created
    await created.close()
    db_client._pool = None


@pytest.mark.asyncio
async def test_get_or_create_instrument_is_idempotent(pool):
    from app.db.market_data import get_or_create_instrument

    symbol = "ZZTEST_IDEMPOTENT"
    try:
        first_id = await get_or_create_instrument(symbol)
        second_id = await get_or_create_instrument(symbol)
        assert first_id == second_id
    finally:
        await pool.execute("DELETE FROM market_instruments WHERE symbol = $1", symbol)


@pytest.mark.asyncio
async def test_insert_and_retrieve_bars_round_trip_correctly(pool):
    from app.db.market_data import get_or_create_instrument, get_recent_bars, insert_bars
    from app.indicators import generate_synthetic_ohlcv

    symbol = "ZZTEST_BARS"
    try:
        instrument_id = await get_or_create_instrument(symbol)
        bars = generate_synthetic_ohlcv(symbol, 30)
        await insert_bars(instrument_id, bars)

        retrieved = await get_recent_bars(instrument_id, 30)
        assert len(retrieved) == 30
        # Chronological order (oldest first) is the indicator engine's
        # expected input shape — verified directly, not assumed.
        timestamps = [b.timestamp for b in retrieved]
        assert timestamps == sorted(timestamps)
        # Values round-trip through Postgres numeric columns without
        # silent precision loss beyond ordinary float tolerance.
        assert abs(retrieved[-1].close - bars[-1].close) < 0.01
    finally:
        await pool.execute(
            "DELETE FROM market_data WHERE instrument_id IN "
            "(SELECT id FROM market_instruments WHERE symbol = $1)",
            symbol,
        )
        await pool.execute("DELETE FROM market_instruments WHERE symbol = $1", symbol)


@pytest.mark.asyncio
async def test_get_recent_bars_respects_the_limit(pool):
    from app.db.market_data import get_or_create_instrument, get_recent_bars, insert_bars
    from app.indicators import generate_synthetic_ohlcv

    symbol = "ZZTEST_LIMIT"
    try:
        instrument_id = await get_or_create_instrument(symbol)
        await insert_bars(instrument_id, generate_synthetic_ohlcv(symbol, 60))
        limited = await get_recent_bars(instrument_id, 10)
        assert len(limited) == 10
    finally:
        await pool.execute(
            "DELETE FROM market_data WHERE instrument_id IN "
            "(SELECT id FROM market_instruments WHERE symbol = $1)",
            symbol,
        )
        await pool.execute("DELETE FROM market_instruments WHERE symbol = $1", symbol)


@pytest.mark.asyncio
async def test_get_or_seed_model_upserts_without_duplicating(pool):
    """
    Requires the 0002 migration (unique constraint on provider_id,
    model_id) — this is exactly the regression that migration exists to
    prevent: without it, this test would insert a second model row on
    the second call instead of updating the first.
    """
    from app.db.signals import get_or_seed_model

    provider_name = "zztest-provider"
    try:
        first = await get_or_seed_model(provider_name, "zztest-model-v1", "Display One")
        second = await get_or_seed_model(provider_name, "zztest-model-v1", "Display Two")
        assert first.model_db_id == second.model_db_id
        assert first.provider_id == second.provider_id

        row = await pool.fetchrow(
            "SELECT display_name FROM ai_models WHERE id = $1", first.model_db_id
        )
        assert row["display_name"] == "Display Two"  # the update side of the upsert took effect
    finally:
        await pool.execute(
            "DELETE FROM ai_models WHERE provider_id IN "
            "(SELECT id FROM ai_providers WHERE name = $1)",
            provider_name,
        )
        await pool.execute("DELETE FROM ai_providers WHERE name = $1", provider_name)


@pytest.mark.asyncio
async def test_save_signal_only_persists_a_setup_found_result(pool):
    from app.db.market_data import get_or_create_instrument
    from app.db.signals import get_or_seed_model, save_signal
    from app.indicators import compute_indicators, generate_synthetic_ohlcv
    from app.providers.base import ProviderReasoning
    from app.verify import build_trade_analysis

    symbol = "ZZTEST_SIGNAL"
    provider_name = "zztest-signal-provider"
    try:
        instrument_id = await get_or_create_instrument(symbol)
        seeded = await get_or_seed_model(provider_name, "zztest-model", provider_name)
        bars = generate_synthetic_ohlcv(symbol, 60)
        indicators = compute_indicators(bars)

        no_setup = build_trade_analysis(
            indicators,
            ProviderReasoning(direction=None, confidence=None, reasoning_summary="x", supporting_evidence=[]),
            "mock", "mock", "mock-v1", True, bars[-1].timestamp, False,
        )
        assert await save_signal(instrument_id, seeded.model_db_id, no_setup) is None

        setup = build_trade_analysis(
            indicators,
            ProviderReasoning(direction="LONG", confidence=70, reasoning_summary="x", supporting_evidence=["rsi14"]),
            "mock", "mock", "mock-v1", True, bars[-1].timestamp, False,
        )
        if setup.status == "SETUP_FOUND":
            signal_id = await save_signal(instrument_id, seeded.model_db_id, setup)
            assert signal_id is not None
            row = await pool.fetchrow("SELECT direction FROM trading_signals WHERE id = $1", signal_id)
            assert row["direction"] == "LONG"
    finally:
        await pool.execute(
            "DELETE FROM trading_signals WHERE instrument_id IN "
            "(SELECT id FROM market_instruments WHERE symbol = $1)",
            symbol,
        )
        await pool.execute("DELETE FROM market_instruments WHERE symbol = $1", symbol)
        await pool.execute(
            "DELETE FROM ai_models WHERE provider_id IN "
            "(SELECT id FROM ai_providers WHERE name = $1)",
            provider_name,
        )
        await pool.execute("DELETE FROM ai_providers WHERE name = $1", provider_name)


@pytest.mark.asyncio
async def test_comparison_full_round_trip_save_get_and_rate(pool):
    """
    The complete flow the PATCH /api/ai/comparisons/{id} endpoint
    depends on: save a comparison with its per-provider results, read it
    back, then update its rating and preferred provider and confirm both
    persisted — exercising the corrected Passage 1 §9.1 ai_comparisons
    schema (provider_set jsonb, not the old hardcoded two-column design)
    against a real database rather than only against mocked calls.
    """
    from app.db.comparisons import (
        get_comparison,
        get_provider_db_id_by_name,
        save_comparison,
        update_comparison_feedback,
    )

    provider_ids = ["zztest-mock", "zztest-grok"]
    try:
        comparison_id = await save_comparison(
            provider_ids,
            {"zztest-mock": {"dataCompleteness": 100.0}, "zztest-grok": {"unavailable": "no key"}},
            {"zztest-mock": None, "zztest-grok": None},
            {"zztest-mock": "mock-v1", "zztest-grok": "grok-4.6"},
        )
        assert comparison_id is not None

        fetched = await get_comparison(comparison_id)
        assert fetched is not None
        assert sorted(fetched["providerSet"]) == sorted(provider_ids)
        assert fetched["userRating"] is None

        preferred_db_id = await get_provider_db_id_by_name("zztest-mock")
        assert preferred_db_id is not None

        updated = await update_comparison_feedback(comparison_id, 5, preferred_db_id)
        assert updated is not None
        assert updated["userRating"] == 5
        assert updated["userChoiceProviderId"] == "zztest-mock"

        # A second partial update (rating only) must not clobber the
        # preference already set — the COALESCE behaviour, checked live.
        updated_again = await update_comparison_feedback(comparison_id, 3, None)
        assert updated_again is not None
        assert updated_again["userRating"] == 3
        assert updated_again["userChoiceProviderId"] == "zztest-mock"
    finally:
        await pool.execute(
            "DELETE FROM ai_comparison_results WHERE provider_id IN "
            "(SELECT id FROM ai_providers WHERE name = ANY($1))",
            provider_ids,
        )
        await pool.execute(
            "DELETE FROM ai_comparisons WHERE provider_set::text LIKE '%zztest%'"
        )
        await pool.execute(
            "DELETE FROM ai_models WHERE provider_id IN "
            "(SELECT id FROM ai_providers WHERE name = ANY($1))",
            provider_ids,
        )
        await pool.execute("DELETE FROM ai_providers WHERE name = ANY($1)", provider_ids)


@pytest.mark.asyncio
async def test_get_comparison_returns_none_for_a_nonexistent_id(pool):
    from app.db.comparisons import get_comparison

    result = await get_comparison("00000000-0000-0000-0000-000000000000")
    assert result is None


@pytest.mark.asyncio
async def test_update_comparison_feedback_returns_none_for_a_nonexistent_id(pool):
    from app.db.comparisons import update_comparison_feedback

    result = await update_comparison_feedback("00000000-0000-0000-0000-000000000000", 5, None)
    assert result is None


@pytest.mark.asyncio
async def test_migrations_produced_the_expected_table_count(pool):
    """
    A cheap, direct sanity check that this test is actually running
    against a fully-migrated schema, not a partially-set-up database
    that would make every other test in this file misleading.
    """
    count = await pool.fetchval(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
    )
    assert count >= 38