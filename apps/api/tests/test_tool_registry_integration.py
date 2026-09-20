"""
Tool registry integration tests against a live Postgres. Skipped, not
failed, without DATABASE_URL, matching every other integration test file
in this suite.

The centrepiece (`test_technical_indicator_tool_reads_back_what_the_engine_persisted`)
is the full loop this phase is built around: the deterministic engine
computes a bundle, `db/indicators_db.py` persists it, and the Technical
Indicator tool — going through the complete governed `execute()` path,
not a shortcut — reads exactly that back. That is what makes "read-only
access to already-computed indicator values" (Passage 4 §3.5/G05) a
tested, literal fact rather than a docstring's claim.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL is not set — tool registry integration tests require a live Postgres instance",
)


@pytest.fixture
async def pool():
    import app.db.client as db_client

    db_client._pool = None
    from app.db.client import get_pool

    p = await get_pool()
    yield p
    db_client._pool = None


@pytest.fixture
async def seeded_registry(pool):
    from app.tools.registry import ToolRegistry
    from app.tools.builtin import ALL_TOOLS

    registry = ToolRegistry()
    for tool in ALL_TOOLS:
        registry.register(tool)
    await registry.seed_database()
    return registry


@pytest.mark.asyncio
async def test_seed_database_writes_all_fifteen_rows(seeded_registry, pool):
    count = await pool.fetchval("SELECT count(*) FROM tool_registry")
    assert count >= 15  # >= since re-runs across test files upsert, never duplicate


@pytest.mark.asyncio
async def test_seed_database_is_idempotent(seeded_registry, pool):
    before = await pool.fetchval("SELECT count(*) FROM tool_registry")
    await seeded_registry.seed_database()
    after = await pool.fetchval("SELECT count(*) FROM tool_registry")
    assert before == after


@pytest.mark.asyncio
async def test_a_successful_call_is_logged_to_tool_calls(seeded_registry, pool):
    from app.tools.schemas import ToolContext

    outcome = await seeded_registry.execute(
        "risk_calculator",
        {"direction": "LONG", "entry": 100.0, "atr": 2.0},
        ToolContext(provider_id="mock"),
    )
    assert outcome.status == "ok"

    row = await pool.fetchrow(
        """
        SELECT tc.status, tc.error, tc.latency_ms
        FROM tool_calls tc
        JOIN tool_registry t ON t.id = tc.tool_id
        WHERE t.name = 'risk_calculator'
        ORDER BY tc.created_at DESC LIMIT 1
        """
    )
    assert row is not None
    assert row["status"] == "ok"
    assert row["error"] is None


@pytest.mark.asyncio
async def test_a_failed_call_is_also_logged_with_its_error(seeded_registry, pool):
    from app.tools.schemas import ToolContext

    await seeded_registry.execute(
        "risk_calculator", {"direction": "LONG"}, ToolContext(provider_id="mock")
    )  # missing required 'atr' and 'entry'

    row = await pool.fetchrow(
        """
        SELECT tc.status, tc.error
        FROM tool_calls tc
        JOIN tool_registry t ON t.id = tc.tool_id
        WHERE t.name = 'risk_calculator' AND tc.status = 'error'
        ORDER BY tc.created_at DESC LIMIT 1
        """
    )
    assert row is not None
    assert "schema validation" in row["error"]


@pytest.mark.asyncio
async def test_technical_indicator_tool_reads_back_what_the_engine_persisted(seeded_registry, pool):
    """The full loop: engine computes -> db/indicators_db.py persists -> the
    governed tool call reads exactly that back."""
    from app.db.indicators_db import persist_indicator_bundle
    from app.db.market_data import get_or_create_instrument
    from app.engine.pipeline import run_engine
    from app.indicators import generate_synthetic_ohlcv
    from app.tools.schemas import ToolContext

    symbol = f"ZZT_TOOL_{uuid.uuid4().hex[:8].upper()}"
    try:
        bars = generate_synthetic_ohlcv(symbol, 90)
        engine_output = run_engine(bars)
        instrument_id = await get_or_create_instrument(symbol)
        await persist_indicator_bundle(
            instrument_id, bars[-1].timestamp, "1d", engine_output.indicators
        )

        outcome = await seeded_registry.execute(
            "technical_indicator", {"symbol": symbol}, ToolContext(provider_id="mock")
        )
        assert outcome.status == "ok"
        assert outcome.available is True
        assert "rsi14" in outcome.output["indicators"]
        assert outcome.output["indicators"]["rsi14"] == pytest.approx(
            engine_output.indicators.rsi14, rel=1e-6
        )
    finally:
        await pool.execute(
            "DELETE FROM technical_indicators WHERE instrument_id IN "
            "(SELECT id FROM market_instruments WHERE symbol = $1)",
            symbol,
        )
        await pool.execute("DELETE FROM market_instruments WHERE symbol = $1", symbol)


@pytest.mark.asyncio
async def test_database_search_tool_finds_a_real_persisted_signal(seeded_registry, pool):
    from app.db.market_data import get_or_create_instrument
    from app.db.signals import get_or_seed_model, save_signal
    from app.indicators import compute_indicators, generate_synthetic_ohlcv
    from app.providers.base import ProviderReasoning
    from app.tools.schemas import ToolContext
    from app.verify import build_trade_analysis

    symbol = f"ZZT_SRCH_{uuid.uuid4().hex[:8].upper()}"
    provider_name = "zztest-search-provider"
    try:
        instrument_id = await get_or_create_instrument(symbol)
        seeded = await get_or_seed_model(provider_name, "zztest-model", provider_name)
        bars = generate_synthetic_ohlcv(symbol, 90)
        indicators = compute_indicators(bars)
        analysis = build_trade_analysis(
            indicators,
            ProviderReasoning(
                direction="LONG", confidence=70, reasoning_summary="x", supporting_evidence=["rsi14"]
            ),
            "mock", provider_name, "zztest-model", True, bars[-1].timestamp, False,
        )
        if analysis.status != "SETUP_FOUND":
            pytest.skip("synthetic data did not produce a setup this run — not this test's concern")
        await save_signal(instrument_id, seeded.model_db_id, analysis)

        outcome = await seeded_registry.execute(
            "database_search", {"symbol": symbol}, ToolContext(provider_id="mock")
        )
        assert outcome.status == "ok"
        assert outcome.output["count"] >= 1
        assert outcome.output["signals"][0]["symbol"] == symbol
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
async def test_mock_provider_cross_check_reports_the_persisted_indicator_count(seeded_registry, pool, monkeypatch):
    """
    Confirms `mock_provider.py`'s real tool call (not the registry
    directly) works end to end: it should find and mention the just-
    persisted indicators as supporting evidence when the full analyze
    flow has run first.
    """
    import app.tools.registry as tools_registry_module

    monkeypatch.setattr(tools_registry_module, "_registry", seeded_registry)

    from app.db.indicators_db import persist_indicator_bundle
    from app.db.market_data import get_or_create_instrument
    from app.engine.pipeline import run_engine
    from app.indicators import generate_synthetic_ohlcv
    from app.providers.base import AnalysisContext
    from app.providers.mock_provider import mock_provider

    symbol = f"ZZT_MOCK_{uuid.uuid4().hex[:8].upper()}"
    try:
        bars = generate_synthetic_ohlcv(symbol, 90)
        engine_output = run_engine(bars)
        instrument_id = await get_or_create_instrument(symbol)
        await persist_indicator_bundle(
            instrument_id, bars[-1].timestamp, "1d", engine_output.indicators
        )

        context = AnalysisContext(
            symbol=symbol,
            indicators=engine_output.indicators,
            last_bars=[{"timestamp": b.timestamp, "close": b.close} for b in bars[-5:]],
            regime=engine_output.regime.model_dump(),
            structure=engine_output.structure.model_dump(),
        )
        reasoning = await mock_provider.reason(context)
        assert any("technical_indicator tool confirmed" in e for e in reasoning.supporting_evidence)
    finally:
        await pool.execute(
            "DELETE FROM technical_indicators WHERE instrument_id IN "
            "(SELECT id FROM market_instruments WHERE symbol = $1)",
            symbol,
        )
        await pool.execute("DELETE FROM market_instruments WHERE symbol = $1", symbol)