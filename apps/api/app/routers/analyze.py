"""
Port of app/api/analyze/route.ts. This is the API Gateway endpoint the
blueprint's architecture diagram (Passage 1 §3) shows the Web/Mobile UI
calling directly over HTTPS — no Next.js intermediary.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from ..comparison import run_comparison, validate_provider_set
from ..db.client import is_db_configured
from ..db.comparisons import build_scores_record, save_comparison
from ..db.market_data import get_or_create_instrument, get_recent_bars, insert_bars
from ..db.signals import get_or_seed_model, save_signal
from ..indicators import compute_indicators, generate_synthetic_ohlcv
from ..providers.registry import resolve_provider
from ..schemas import AnalyzeRequest, ComparisonResponse, TradeAnalysis, now_iso
from ..verify import build_trade_analysis

router = APIRouter()

BARS_NEEDED = 60


def resolve_model_id(provider_id: str) -> str:
    if provider_id == "gemma":
        return os.environ.get("GEMMA_MODEL_ID", "gemma-4-4b-it")
    if provider_id == "grok":
        return os.environ.get("GROK_MODEL_ID", "grok-4.6")
    if provider_id == "custom":
        return os.environ.get("CUSTOM_AI_MODEL_ID", "unset")
    return "mock-v1"


async def get_or_refresh_bars(symbol: str):
    """
    Shared by both single- and multi-provider modes. Reads persisted bars
    from the database when configured; generates and stores a fresh
    (still-synthetic) set the first time a symbol is requested; falls
    back to ephemeral synthetic data — cleanly, not a crash — if the
    database isn't configured or a call to it fails.
    """
    if not is_db_configured():
        return generate_synthetic_ohlcv(symbol, BARS_NEEDED), None, False
    try:
        instrument_id = await get_or_create_instrument(symbol)
        existing = await get_recent_bars(instrument_id, BARS_NEEDED)
        if len(existing) >= BARS_NEEDED:
            return existing, instrument_id, True
        bars = generate_synthetic_ohlcv(symbol, BARS_NEEDED)
        await insert_bars(instrument_id, bars)
        return bars, instrument_id, True
    except Exception as err:  # noqa: BLE001 — deliberate: any DB failure degrades, never crashes the request
        print(f"DB path failed, falling back to ephemeral synthetic data: {err}")
        return generate_synthetic_ohlcv(symbol, BARS_NEEDED), None, False


@router.post("/api/ai/analyze")
async def analyze(body: AnalyzeRequest):
    symbol = body.symbol.upper()
    bars, instrument_id, persisted = await get_or_refresh_bars(symbol)
    indicators = compute_indicators(bars)
    last_bars = [{"timestamp": b.timestamp, "close": b.close} for b in bars[-5:]]

    # ---- Multi-provider comparison mode ----
    if body.providers:
        validation_error = validate_provider_set(body.providers)
        if validation_error:
            raise HTTPException(status_code=400, detail=validation_error)

        results = await run_comparison(body.providers, symbol, indicators, last_bars, resolve_model_id)

        if persisted and instrument_id:
            try:
                signal_id_by_provider: dict[str, str | None] = {}
                model_id_by_provider: dict[str, str] = {}
                for slot in results:
                    model_id_by_provider[slot.providerId] = resolve_model_id(slot.providerId)
                    if slot.outcome == "ok" and slot.analysis.status == "SETUP_FOUND":
                        seeded = await get_or_seed_model(
                            slot.providerId, resolve_model_id(slot.providerId), slot.providerId
                        )
                        signal_id_by_provider[slot.providerId] = await save_signal(
                            instrument_id, seeded.model_db_id, slot.analysis
                        )
                    else:
                        signal_id_by_provider[slot.providerId] = None
                await save_comparison(
                    body.providers,
                    build_scores_record(results),
                    signal_id_by_provider,
                    model_id_by_provider,
                )
            except Exception as err:  # noqa: BLE001
                # The comparison result itself is still valid and already
                # computed — a failed write shouldn't turn a good
                # response into a 503, same principle as the
                # single-provider path below.
                print(f"Failed to persist comparison (results still returned): {err}")

        return ComparisonResponse(
            mode="multi",
            symbol=symbol,
            results=results,
            persisted=persisted,
            generatedAt=now_iso(),
        )

    # ---- Single-provider mode (unchanged contract) ----
    provider = resolve_provider(body.provider)
    if provider.id == "mock":
        source = "mock"
    elif provider.id == "custom" and os.environ.get("CUSTOM_AI_SOURCE_TAG") == "local_model":
        source = "local_model"
    else:
        source = "hosted_api"
    model_id = resolve_model_id(provider.id)

    try:
        from ..providers.base import AnalysisContext

        reasoning = await provider.reason(AnalysisContext(symbol=symbol, indicators=indicators, last_bars=last_bars))
        analysis: TradeAnalysis = build_trade_analysis(indicators, reasoning, source, provider.id, model_id, persisted)

        if persisted and instrument_id and analysis.status == "SETUP_FOUND":
            try:
                seeded = await get_or_seed_model(provider.id, model_id, provider.display_name)
                await save_signal(instrument_id, seeded.model_db_id, analysis)
            except Exception as err:  # noqa: BLE001
                print(f"Failed to persist signal (analysis still returned): {err}")

        return analysis
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Provider call failed: {err}") from err
