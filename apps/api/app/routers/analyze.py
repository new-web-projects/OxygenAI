"""
POST /api/ai/analyze — Passage 1 §3's architecture diagram: the endpoint
the Web/Mobile UI calls directly over HTTPS, no Next.js intermediary.

Rewired this pass to close the same integration gap `comparison.py` had:
this router previously called `compute_indicators()` for the four base
indicators and `provider.reason()` directly, so the full indicator
library, the regime classifier, market structure, the Risk Engine, and
the circuit breaker built this pass were all present in the codebase but
unreachable from the one endpoint that actually serves requests. Single-
provider mode now runs the same `run_engine()` pipeline and
`execute_provider()` path multi-provider mode uses, so both modes share
one deterministic-engine call and one execution path rather than two
that could silently drift apart.

Model resolution now goes through `providers.model_registry.
configured_model_id`, which is registry-aware (Grok's retired-slug
redirects, §3.2/G02) rather than the router's own hardcoded per-provider
defaults from before this pass.

Auth (this pass): accepts but does not require a bearer token
(`security.dependencies.get_optional_user`). Passage 4 §6.2 states
analyze requires JWT; making it mandatory here today would break the
one endpoint the live, working frontend actually depends on, since that
frontend has no login flow yet — a deliberate, disclosed interim step,
not a claim that full enforcement is done. When a token is present, the
request is attributed via a new `usage_logs` write (see `db/usage.py`);
anonymous requests behave exactly as before this pass.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from ..comparison import run_comparison, validate_provider_set
from ..db.client import is_db_configured
from ..db.comparisons import build_scores_record, save_comparison
from ..db.indicators_db import persist_indicator_bundle
from ..db.market_data import get_or_create_instrument, get_recent_bars, insert_bars
from ..db.signals import get_or_seed_model, save_signal
from ..db.usage import record_usage
from ..engine.pipeline import run_engine
from ..errors import MarketDataUnavailableError, ProviderUnavailableError, ValidationError
from ..indicators import generate_synthetic_ohlcv
from ..observability import get_logger
from ..providers.base import AnalysisContext
from ..providers.circuit_breaker import CircuitBreakerOpen
from ..providers.model_registry import configured_model_id
from ..providers.registry import execute_provider, resolve_provider
from ..schemas import AnalyzeRequest, ComparisonResponse, ComparisonSlotOk, TradeAnalysis, now_iso
from ..security.dependencies import AuthenticatedUser, get_optional_user
from ..utils import compute_freshness
from ..verify import build_trade_analysis

router = APIRouter()

BARS_NEEDED = 60

logger = get_logger("routers.analyze")


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
        logger.warning(
            "DB path failed, falling back to ephemeral synthetic data", extra={"error": str(err)}
        )
        return generate_synthetic_ohlcv(symbol, BARS_NEEDED), None, False


@router.post("/api/ai/analyze")
async def analyze(body: AnalyzeRequest, user: AuthenticatedUser | None = Depends(get_optional_user)):
    symbol = body.symbol.upper()
    bars, instrument_id, persisted = await get_or_refresh_bars(symbol)

    try:
        engine_output = run_engine(
            bars,
            account_equity=body.accountEquity,
            max_risk_per_trade_pct=body.maxRiskPerTradePct,
        )
    except ValueError as err:
        # Passage 4 §6.2: 409 — market data problems are a conflict, not
        # a client input-validation error.
        raise MarketDataUnavailableError(str(err)) from err

    last_bars = [{"timestamp": b.timestamp, "close": b.close} for b in bars[-5:]]

    # Data freshness (Passage 4 §3.6): a property of the underlying bars,
    # not of any one provider's reasoning — computed once, shared by
    # every slot in both modes below.
    data_timestamp = bars[-1].timestamp
    is_stale = compute_freshness(data_timestamp)

    if persisted and instrument_id:
        try:
            # First write `technical_indicators` has ever received
            # (Passage 4 §3.5/G05's Technical Indicator tool reads this
            # back rather than recomputing — see db/indicators_db.py).
            await persist_indicator_bundle(
                instrument_id, data_timestamp, body.timeframe, engine_output.indicators
            )
        except Exception as err:  # noqa: BLE001 — best-effort, matches every other DB write here
            logger.warning("Failed to persist indicators", extra={"error": str(err)})

    # ---- Multi-provider comparison mode ----
    if body.is_multi():
        provider_list = body.providers or []
        validation_error = validate_provider_set(provider_list)
        if validation_error:
            raise ValidationError(validation_error)

        started = time.perf_counter()
        results = await run_comparison(
            provider_list,
            symbol,
            engine_output.indicators,
            last_bars,
            configured_model_id,
            persisted,
            data_timestamp,
            is_stale,
            risk_settings=engine_output.risk_settings,
            regime_info=engine_output.regime,
            structure_info=engine_output.structure,
        )
        total_latency_ms = round((time.perf_counter() - started) * 1000, 2)
        provider_latencies = [
            slot.latencyMs
            for slot in results
            if isinstance(slot, ComparisonSlotOk) and slot.latencyMs is not None
        ]

        comparison_id: str | None = None
        if persisted and instrument_id:
            try:
                signal_id_by_provider: dict[str, str | None] = {}
                model_id_by_provider: dict[str, str] = {}
                for slot in results:
                    model_id_by_provider[slot.providerId] = configured_model_id(slot.providerId)
                    if slot.outcome == "ok" and slot.analysis.status == "SETUP_FOUND":
                        seeded = await get_or_seed_model(
                            slot.providerId, model_id_by_provider[slot.providerId], slot.providerId
                        )
                        signal_id_by_provider[slot.providerId] = await save_signal(
                            instrument_id, seeded.model_db_id, slot.analysis
                        )
                    else:
                        signal_id_by_provider[slot.providerId] = None
                comparison_id = await save_comparison(
                    provider_list,
                    build_scores_record(results),
                    signal_id_by_provider,
                    model_id_by_provider,
                )
            except Exception as err:  # noqa: BLE001
                # The comparison result itself is still valid and already
                # computed — a failed write shouldn't turn a good
                # response into a 503, same principle as the
                # single-provider path below.
                logger.warning(
                    "Failed to persist comparison (results still returned)",
                    extra={"error": str(err)},
                )

        return ComparisonResponse(
            mode="multi",
            symbol=symbol,
            results=results,
            persisted=persisted,
            generatedAt=now_iso(),
            comparisonId=comparison_id,
            totalLatencyMs=total_latency_ms,
            maxProviderLatencyMs=max(provider_latencies) if provider_latencies else None,
            sumProviderLatencyMs=round(sum(provider_latencies), 2) if provider_latencies else None,
        )

    # ---- Single-provider mode ----
    provider = resolve_provider(body.single_provider_id())
    context = AnalysisContext(
        symbol=symbol,
        indicators=engine_output.indicators,
        last_bars=last_bars,
        regime=engine_output.regime.model_dump(),
        structure=engine_output.structure.model_dump(),
    )

    try:
        # Passage 4 §6.2: "503 provider unavailable, fallback attempted
        # first" — allow_fallback=True tries mock before giving up, so a
        # transient failure of a real provider still returns a usable
        # (if less specific) result rather than an outright error.
        result = await execute_provider(provider.id, context, allow_fallback=True)
    except CircuitBreakerOpen as err:
        raise ProviderUnavailableError(str(err)) from err
    except Exception as err:  # noqa: BLE001
        raise ProviderUnavailableError(f"Provider call failed: {err}") from err

    analysis: TradeAnalysis = build_trade_analysis(
        engine_output.indicators,
        result.reasoning,
        result.source_tag,  # type: ignore[arg-type]
        result.provider_id,
        result.model_id,
        persisted,
        data_timestamp,
        is_stale,
        risk_settings=engine_output.risk_settings,
        regime=engine_output.regime,
        structure=engine_output.structure,
        transport=result.source_tag,  # type: ignore[arg-type]
        engine_warnings=engine_output.warnings,
    )

    if persisted and instrument_id:
        try:
            seeded = await get_or_seed_model(
                result.provider_id, result.model_id, provider.display_name
            )
            if analysis.status == "SETUP_FOUND":
                await save_signal(instrument_id, seeded.model_db_id, analysis)
            if user is not None:
                # Passage 1 §3's auth requirement, applied where it can be
                # today: `/api/ai/analyze` accepts but does not require a
                # token (see this file's module docstring). When one is
                # present, the request is attributed here — the first
                # write `usage_logs` has ever received.
                await record_usage(user.id, seeded.provider_id, seeded.model_db_id)
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "Failed to persist signal/usage (analysis still returned)",
                extra={"error": str(err)},
            )

    return analysis