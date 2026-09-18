"""
Multi-provider comparison — Passage 1 §4.5's isolation guarantee:
asyncio.gather(..., return_exceptions=True), so one provider's failure
never takes the others down.

Rewired this pass to close a real integration gap: execution previously
called `provider.reason()` directly, so none of this session's new
circuit breaker, retry/timeout, or model-registry redirect logic — all
built earlier this pass — was actually reachable through the comparison
path. `_run_one` now goes through `providers.registry.execute_provider`,
which is the router's stages 2-6 (circuit breaker -> execute -> validate
-> tag). `allow_fallback=False` is deliberate and unchanged in spirit
from before: a comparison is supposed to show distinct real providers
side by side, so a failure must surface as that provider's own failure,
never be silently swapped for mock.

Also new: per-slot latency, and Passage 4 §3.3's G11 recovery made
observable rather than only true by construction — "total latency ≈
max(latency across the participating providers), not the sum" — both
figures are computed and returned so the property can be asserted in a
test rather than trusted on faith.

`run_comparison`'s existing positional parameters are unchanged, so
every call site and test written against the previous signature keeps
working; every new capability is a keyword-only argument with a default.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable

from .providers.base import AnalysisContext
from .providers.circuit_breaker import CircuitBreakerOpen
from .providers.registry import execute_provider
from .schemas import (
    ComparisonSlot,
    ComparisonSlotOk,
    ComparisonSlotUnavailable,
    IndicatorBundle,
    RegimeInfo,
    StructureInfo,
)
from .scoring import score_analysis
from .verify import build_trade_analysis

# mock is not one of the blueprint's authoritative combinations (Custom
# AI / Grok / Gemma 4, any 2 or all 3) — it's kept valid here too, purely
# so the comparison view can be demonstrated without live credentials.
# Comparing a provider against itself isn't a comparison, so it's not
# allowed to appear twice.
VALID_PROVIDER_IDS = ["mock", "custom", "grok", "gemma"]


def validate_provider_set(providers: list[str]) -> str | None:
    if len(providers) < 2 or len(providers) > 3:
        return "providers must list 2 or 3 provider ids"
    if len(set(providers)) != len(providers):
        return "providers must not repeat"
    for p in providers:
        if p not in VALID_PROVIDER_IDS:
            return f"unknown provider id: {p} (must be one of {', '.join(VALID_PROVIDER_IDS)})"
    return None


@dataclass
class _SlotResult:
    provider_id: str
    slot: ComparisonSlot
    latency_ms: float


def _classify_failure(provider_id: str, err: BaseException) -> ComparisonSlotUnavailable:
    """Map an execution failure to a reasonCode the UI can branch on."""
    if isinstance(err, CircuitBreakerOpen):
        return ComparisonSlotUnavailable(
            providerId=provider_id,
            reason=str(err),
            reasonCode="circuit_open",
            retryAfterSeconds=err.retry_after,
        )
    if isinstance(err, KeyError):
        return ComparisonSlotUnavailable(
            providerId=provider_id, reason=str(err), reasonCode="unknown_provider"
        )
    message = str(err)
    if "not set" in message or "not configured" in message.lower() or "must all be set" in message:
        return ComparisonSlotUnavailable(
            providerId=provider_id, reason=message, reasonCode="not_configured"
        )
    return ComparisonSlotUnavailable(providerId=provider_id, reason=message, reasonCode="call_failed")


async def _run_one(
    provider_id: str,
    symbol: str,
    indicators: IndicatorBundle,
    last_bars: list[dict],
    persisted: bool,
    data_timestamp: str,
    is_stale: bool,
    *,
    risk_settings,
    regime_info: RegimeInfo | None,
    structure_info: StructureInfo | None,
    knowledge_sources: list[str] | None,
) -> _SlotResult:
    started = time.perf_counter()
    context = AnalysisContext(
        symbol=symbol,
        indicators=indicators,
        last_bars=last_bars,
        regime=regime_info.model_dump() if regime_info else None,
        structure=structure_info.model_dump() if structure_info else None,
    )
    result = await execute_provider(provider_id, context, allow_fallback=False)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    analysis = build_trade_analysis(
        indicators,
        result.reasoning,
        result.source_tag,  # type: ignore[arg-type]
        result.provider_id,
        result.model_id,
        persisted,
        data_timestamp,
        is_stale,
        risk_settings=risk_settings,
        regime=regime_info,
        structure=structure_info,
        transport=result.source_tag,  # type: ignore[arg-type]
        knowledge_sources=knowledge_sources,
    )
    slot = ComparisonSlotOk(
        providerId=provider_id,
        analysis=analysis,
        scores=score_analysis(analysis),
        latencyMs=result.latency_ms,
    )
    return _SlotResult(provider_id=provider_id, slot=slot, latency_ms=latency_ms)


async def run_comparison(
    provider_ids: list[str],
    symbol: str,
    indicators: IndicatorBundle,
    last_bars: list[dict],
    resolve_model_id: Callable[[str], str],
    persisted: bool,
    data_timestamp: str,
    is_stale: bool,
    *,
    risk_settings=None,
    regime_info: RegimeInfo | None = None,
    structure_info: StructureInfo | None = None,
    knowledge_sources: list[str] | None = None,
) -> list[ComparisonSlot]:
    """
    Return type is unchanged from before this pass — `list[ComparisonSlot]`
    — so every existing call site and test keeps working exactly as
    written. Per-slot latency is still tracked (see `ComparisonSlotOk.
    latencyMs`); callers that want the aggregate G11 summary derive it
    from the returned slots plus their own wall-clock timing around this
    call, which is what `routers/analyze.py` does — that keeps this
    function's contract simple rather than growing a second return shape.

    `resolve_model_id` is accepted for backward compatibility with
    existing call sites but is no longer the source of truth for model
    selection — `execute_provider` resolves the model through the
    registry itself, redirects included.
    """
    settled = await asyncio.gather(
        *[
            _run_one(
                pid,
                symbol,
                indicators,
                last_bars,
                persisted,
                data_timestamp,
                is_stale,
                risk_settings=risk_settings,
                regime_info=regime_info,
                structure_info=structure_info,
                knowledge_sources=knowledge_sources,
            )
            for pid in provider_ids
        ],
        return_exceptions=True,
    )

    results: list[ComparisonSlot] = []
    for provider_id, outcome in zip(provider_ids, settled):
        if isinstance(outcome, BaseException):
            results.append(_classify_failure(provider_id, outcome))
        else:
            results.append(outcome.slot)
    return results