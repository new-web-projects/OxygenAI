"""
Port of lib/comparison.ts. Runs every requested provider concurrently and
isolates failures per slot — this is Passage 1 §4.5's isolation guarantee,
literally: asyncio.gather(..., return_exceptions=True). The TypeScript
version used Promise.allSettled as the JS equivalent of this; here it's
the real thing the blueprint actually specifies.
"""

from __future__ import annotations

import asyncio

from .providers.base import AnalysisContext
from .providers.registry import get_provider_strict
from .schemas import ComparisonSlot, ComparisonSlotOk, ComparisonSlotUnavailable, IndicatorBundle
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


async def _run_one(provider_id: str, symbol: str, indicators: IndicatorBundle, last_bars: list[dict], model_id: str) -> ComparisonSlotOk:
    provider = get_provider_strict(provider_id)
    reasoning = await provider.reason(
        AnalysisContext(symbol=symbol, indicators=indicators, last_bars=last_bars)
    )
    source = "mock" if provider_id == "mock" else "hosted_api"
    analysis = build_trade_analysis(indicators, reasoning, source, provider_id, model_id, False)
    return ComparisonSlotOk(providerId=provider_id, analysis=analysis, scores=score_analysis(analysis))


async def run_comparison(
    provider_ids: list[str],
    symbol: str,
    indicators: IndicatorBundle,
    last_bars: list[dict],
    resolve_model_id,
) -> list[ComparisonSlot]:
    settled = await asyncio.gather(
        *[
            _run_one(pid, symbol, indicators, last_bars, resolve_model_id(pid))
            for pid in provider_ids
        ],
        return_exceptions=True,
    )

    results: list[ComparisonSlot] = []
    for provider_id, outcome in zip(provider_ids, settled):
        if isinstance(outcome, BaseException):
            results.append(ComparisonSlotUnavailable(providerId=provider_id, reason=str(outcome)))
        else:
            results.append(outcome)
    return results
