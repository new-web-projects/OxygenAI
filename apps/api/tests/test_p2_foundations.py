"""
Phase P2 — operational foundations that need no database.

Each test here pins a defect that was reproduced against the pre-P2 code (see
docs/implementation/PLAN.md, section D):

* a provider that is merely *not configured* was counted by the circuit
  breaker, so a few comparisons later it reported `circuit_open`;
* a LONG whose ATR stop lands at or below zero (and a SHORT whose far targets
  do) was accepted as a valid setup;
* two simultaneous cold-start requests each created a connection pool and
  orphaned one;
* the pool was never closed on shutdown and nothing ran the retention job.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import RiskSettings
from app.engine.risk_engine import assess
from app.providers.base import AnalysisContext, ProviderNotConfiguredError


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    # (conftest.py already resets the circuit breakers around every test.)
    for name in (
        "XAI_API_KEY",
        "GOOGLE_AI_API_KEY",
        "GEMMA_TRANSPORT",
        "CUSTOM_AI_BASE_URL",
        "CUSTOM_AI_MODEL_ID",
        "CUSTOM_AI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


def _context() -> AnalysisContext:
    from app.indicators import compute_indicators, generate_synthetic_ohlcv

    bars = generate_synthetic_ohlcv("P2UNIT", 60)
    return AnalysisContext(
        symbol="P2UNIT",
        indicators=compute_indicators(bars),
        last_bars=[{"timestamp": b.timestamp, "close": b.close} for b in bars[-5:]],
    )


# --------------------------------------------------------------------------
# Circuit breaker vs. configuration
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unconfigured_provider_is_never_counted_by_the_circuit_breaker():
    from app.providers.circuit_breaker import get_circuit_breakers
    from app.providers.registry import execute_provider

    for _ in range(12):  # the default threshold is 5 failures
        with pytest.raises(ProviderNotConfiguredError) as raised:
            await execute_provider("grok", _context(), allow_fallback=False)
        assert "XAI_API_KEY" in str(raised.value)

    snapshot = await get_circuit_breakers().snapshot("grok")
    assert snapshot.state == "closed"
    assert snapshot.recent_failures == 0
    assert snapshot.total_failures == 0


@pytest.mark.asyncio
async def test_unconfigured_with_fallback_degrades_to_mock_tagged_fallback_and_does_not_trip_the_breaker():
    from app.providers.circuit_breaker import get_circuit_breakers
    from app.providers.registry import execute_provider

    for _ in range(8):
        result = await execute_provider("gemma", _context(), allow_fallback=True)
        assert result.fell_back is True
        assert (result.provider_id, result.model_id, result.source_tag) == ("mock", "mock-v1", "fallback")

    snapshot = await get_circuit_breakers().snapshot("gemma")
    assert snapshot.state == "closed"
    assert snapshot.total_failures == 0


@pytest.mark.asyncio
async def test_comparison_keeps_reporting_not_configured_instead_of_circuit_open():
    from app.comparison import run_comparison
    from app.indicators import compute_indicators, generate_synthetic_ohlcv

    bars = generate_synthetic_ohlcv("P2CMP", 60)
    indicators = compute_indicators(bars)
    last_bars = [{"timestamp": b.timestamp, "close": b.close} for b in bars[-5:]]

    codes: list[str] = []
    for _ in range(10):  # well past the 5-failure threshold that used to open the breaker
        results = await run_comparison(
            ["mock", "grok"], "P2CMP", indicators, last_bars, lambda pid: f"{pid}-m", False, bars[-1].timestamp, False
        )
        by_id = {r.providerId: r for r in results}
        assert by_id["mock"].outcome == "ok"
        codes.append(by_id["grok"].reasonCode)

    assert set(codes) == {"not_configured"}


@pytest.mark.asyncio
async def test_a_real_provider_failure_still_trips_the_breaker(monkeypatch):
    """The fix must not exempt genuine outages — only configuration states."""
    from app.providers.circuit_breaker import CircuitBreakerOpen, get_circuit_breakers
    from app.providers.grok_provider import GrokProvider
    from app.providers.registry import execute_provider

    monkeypatch.setenv("XAI_API_KEY", "test-key-not-real")

    async def boom(self, context):  # noqa: ANN001
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(GrokProvider, "reason", boom)

    for _ in range(5):
        with pytest.raises(RuntimeError, match="upstream 503"):
            await execute_provider("grok", _context(), allow_fallback=False)
    with pytest.raises(CircuitBreakerOpen):
        await execute_provider("grok", _context(), allow_fallback=False)
    assert (await get_circuit_breakers().snapshot("grok")).state == "open"


# --------------------------------------------------------------------------
# Risk engine: prices must be positive
# --------------------------------------------------------------------------


def _settings() -> RiskSettings:
    return RiskSettings(
        account_equity=1_000_000.0,
        max_risk_per_trade_pct=1.0,
        sizing_method="atr_based",
        atr_stop_multiple=1.5,
        min_risk_reward=1.5,
    )


def test_a_long_whose_atr_stop_is_not_a_positive_price_is_rejected():
    result = assess(entry=10.0, atr=8.0, direction="LONG", settings=_settings())  # stop = 10 - 12 = -2
    assert result.stop_loss == -2.0
    assert result.is_tradeable is False
    assert result.targets == []
    assert "at or below zero" in (result.rejected_reason or "")


def test_a_short_keeps_only_the_positive_targets():
    result = assess(entry=100.0, atr=20.0, direction="SHORT", settings=_settings())  # R=30: 40, 10, -20
    assert result.targets == [40.0, 10.0]
    assert all(t > 0 for t in result.targets)
    assert "dropped" in result.target_basis
    assert result.is_tradeable is True


def test_a_short_with_no_reachable_target_is_rejected():
    result = assess(entry=10.0, atr=4.0, direction="SHORT", settings=_settings())  # R=6: -2, -8, -14
    assert result.is_tradeable is False
    assert result.targets == []
    assert "no reachable target" in (result.rejected_reason or "")


def test_an_ordinary_setup_is_unchanged_by_the_guard():
    result = assess(entry=100.0, atr=2.0, direction="LONG", settings=_settings())
    assert result.stop_loss == 97.0
    assert result.targets == [106.0, 109.0, 112.0]
    assert result.is_tradeable is True
    assert "dropped" not in result.target_basis


def _verify(direction: str, atr_multiple_of_price: float):
    from app.indicators import compute_indicators, generate_synthetic_ohlcv
    from app.providers.base import ProviderReasoning
    from app.verify import build_trade_analysis

    bars = generate_synthetic_ohlcv("P2NEG", 60)
    indicators = compute_indicators(bars)
    indicators = indicators.model_copy(update={"atr14": indicators.lastClose * atr_multiple_of_price})
    reasoning = ProviderReasoning(
        direction=direction,  # type: ignore[arg-type]
        confidence=70,
        reasoning_summary="momentum and trend agree",
        supporting_evidence=["rsi14"],
    )
    return build_trade_analysis(
        indicators, reasoning, "mock", "mock", "mock-v1", False, bars[-1].timestamp, False
    )


def test_verification_turns_a_negative_stop_into_no_valid_setup_with_null_prices():
    analysis = _verify("LONG", 2.0)  # stop = price - 1.5 * 2 * price < 0
    assert analysis.status == "NO_VALID_SETUP"
    assert analysis.entry is None and analysis.stopLoss is None and analysis.targets == []
    assert "at or below zero" in analysis.reasoningSummary
    assert analysis.risk is not None and analysis.risk.rejectedReason


def test_verification_turns_an_unreachable_short_into_no_valid_setup():
    analysis = _verify("SHORT", 0.4)  # R = 0.6 * price: every target at 2R or beyond is <= 0
    assert analysis.status == "NO_VALID_SETUP"
    assert analysis.targets == []


# --------------------------------------------------------------------------
# Connection pool
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_cold_start_requests_create_exactly_one_pool(monkeypatch):
    import app.db.client as client

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/none")
    monkeypatch.setattr(client, "_pool", None)
    created: list[object] = []

    async def slow_create_pool(**kwargs):  # noqa: ANN003
        await asyncio.sleep(0.05)  # long enough for every caller to pass the `is None` check
        pool = object()
        created.append(pool)
        return pool

    monkeypatch.setattr(client.asyncpg, "create_pool", slow_create_pool)

    pools = await asyncio.gather(*(client.get_pool() for _ in range(8)))
    assert len(created) == 1
    assert all(p is created[0] for p in pools)
    monkeypatch.setattr(client, "_pool", None)


@pytest.mark.asyncio
async def test_lifespan_closes_the_pool_and_cancels_the_retention_loop(monkeypatch):
    import app.db.client as client
    import app.db.retention as retention
    from app.config import get_settings
    from app.main import _lifespan, app

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/none")
    monkeypatch.setenv("RETENTION_INTERVAL_SECONDS", "0.01")
    get_settings(refresh=True)

    passes = 0

    async def fake_once():
        nonlocal passes
        passes += 1
        return retention.RetentionReport(0, 0)

    closed = 0

    async def fake_close():
        nonlocal closed
        closed += 1

    monkeypatch.setattr(retention, "run_retention_once", fake_once)
    monkeypatch.setattr(client, "close_pool", fake_close)

    async with _lifespan(app):
        await asyncio.sleep(0.12)
    assert passes >= 2, "the loop should have run repeatedly while the app was up"
    assert closed == 1
    settled = passes
    await asyncio.sleep(0.05)
    assert passes == settled, "the loop must be cancelled on shutdown"
    get_settings(refresh=True)


@pytest.mark.asyncio
async def test_retention_is_not_started_when_disabled(monkeypatch):
    import app.db.retention as retention
    from app.config import get_settings
    from app.main import _lifespan, app

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/none")
    monkeypatch.setenv("RETENTION_INTERVAL_SECONDS", "0")
    get_settings(refresh=True)
    called = False

    async def fake_once():
        nonlocal called
        called = True
        return retention.RetentionReport(0, 0)

    monkeypatch.setattr(retention, "run_retention_once", fake_once)
    async with _lifespan(app):
        await asyncio.sleep(0.05)
    assert called is False
    get_settings(refresh=True)


def test_retention_interval_defaults_to_hourly(monkeypatch):
    from app.config import get_settings

    monkeypatch.delenv("RETENTION_INTERVAL_SECONDS", raising=False)
    assert get_settings(refresh=True).retention_interval_seconds == 3600.0
    get_settings(refresh=True)


# --------------------------------------------------------------------------
# Best-effort writers without a database
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writers_are_noops_without_a_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from app.db.audit import record_audit
    from app.db.errors import record_error
    from app.db.health import record_health_check
    from app.db.retention import run_retention_once

    assert await record_audit(actor_id=None, action="x", resource="y") is False
    assert await record_error(error_id="abc", error_type="E", message="m") is False
    assert await record_health_check("mock", "mock-v1", True, 1.0, None) is False
    report = await run_retention_once()
    assert (report.sessions_deleted, report.memories_deleted, report.errors) == (0, 0, 0)


@pytest.mark.asyncio
async def test_record_error_rejects_an_unknown_severity():
    from app.db.errors import record_error

    with pytest.raises(ValueError):
        await record_error(error_id="abc", error_type="E", message="m", severity="catastrophic")


@pytest.mark.asyncio
async def test_a_failing_error_writer_cannot_break_the_500_response(monkeypatch):
    import httpx
    from fastapi import FastAPI

    from app.errors import install_error_handlers

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/none")

    async def broken(**kwargs):  # noqa: ANN003
        raise RuntimeError("database exploded")

    import app.db.errors as db_errors

    monkeypatch.setattr(db_errors, "record_error", broken)

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("original failure")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        response = await c.get("/boom")
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert response.json()["errorId"]


def test_bearer_identity_for_the_error_row_ignores_junk():
    from starlette.requests import Request

    from app.errors import _user_id_from_bearer

    def req(header: str | None) -> Request:
        headers = [(b"authorization", header.encode())] if header else []
        return Request({"type": "http", "headers": headers, "method": "GET", "path": "/"})

    assert _user_id_from_bearer(req(None)) is None
    assert _user_id_from_bearer(req("Bearer not-a-jwt")) is None
    assert _user_id_from_bearer(req("Basic abc")) is None


# --------------------------------------------------------------------------
# GET /health (ENDPOINT_CHECKLIST: "automated test planned (P2)")
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint_reports_ok_without_a_database(monkeypatch):
    import httpx

    from app.main import app

    monkeypatch.delenv("DATABASE_URL", raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        response = await c.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
