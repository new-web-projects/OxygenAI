"""
The verification stage. Port of lib/verify.ts. Entry/stop/targets are
computed HERE, from ATR — never taken from the AI provider's own text.
The provider only gets to propose a direction and a confidence; every
number in the response is this function's responsibility. A consistency
check runs before anything is returned; if it fails, the setup is
suppressed rather than shipped.
"""

from __future__ import annotations

from .providers.base import ProviderReasoning
from .schemas import IndicatorBundle, TradeAnalysis, now_iso
from .utils import round2


def _no_setup(
    indicators: IndicatorBundle,
    reasoning: ProviderReasoning,
    source: str,
    provider_id: str,
    model_id: str,
    persisted: bool,
    data_timestamp: str,
    is_stale: bool,
    reason: str | None = None,
) -> TradeAnalysis:
    return TradeAnalysis(
        status="NO_VALID_SETUP",
        direction=None,
        entry=None,
        stopLoss=None,
        targets=[],
        confidence=None,
        riskReward=None,
        reasoningSummary=reason if reason else reasoning.reasoning_summary,
        supportingEvidence=[] if reason else reasoning.supporting_evidence,
        source=source,  # type: ignore[arg-type]
        provider=provider_id,
        model=model_id,
        indicatorsUsed=indicators,
        generatedAt=now_iso(),
        persisted=persisted,
        dataTimestamp=data_timestamp,
        isStale=is_stale,
    )


def build_trade_analysis(
    indicators: IndicatorBundle,
    reasoning: ProviderReasoning,
    source: str,
    provider_id: str,
    model_id: str,
    persisted: bool,
    data_timestamp: str,
    is_stale: bool,
) -> TradeAnalysis:
    direction = reasoning.direction
    confidence = reasoning.confidence

    if not direction or indicators.atr14 is None:
        return _no_setup(
            indicators, reasoning, source, provider_id, model_id, persisted, data_timestamp, is_stale
        )

    entry = indicators.lastClose
    atr_value = indicators.atr14
    stop_loss = entry - 1.5 * atr_value if direction == "LONG" else entry + 1.5 * atr_value
    target1 = entry + 1.5 * atr_value if direction == "LONG" else entry - 1.5 * atr_value
    target2 = entry + 3 * atr_value if direction == "LONG" else entry - 3 * atr_value

    consistent = stop_loss < entry if direction == "LONG" else stop_loss > entry
    if not consistent:
        return _no_setup(
            indicators,
            reasoning,
            source,
            provider_id,
            model_id,
            persisted,
            data_timestamp,
            is_stale,
            reason="Internal consistency check failed on the computed setup — suppressed rather than returned.",
        )

    risk = abs(entry - stop_loss)
    reward = abs(target1 - entry)
    risk_reward = round2(reward / risk) if risk > 0 else None

    return TradeAnalysis(
        status="SETUP_FOUND",
        direction=direction,  # type: ignore[arg-type]
        entry=round2(entry),
        stopLoss=round2(stop_loss),
        targets=[round2(target1), round2(target2)],
        confidence=confidence,
        riskReward=risk_reward,
        reasoningSummary=reasoning.reasoning_summary,
        supportingEvidence=reasoning.supporting_evidence,
        source=source,  # type: ignore[arg-type]
        provider=provider_id,
        model=model_id,
        indicatorsUsed=indicators,
        generatedAt=now_iso(),
        persisted=persisted,
        dataTimestamp=data_timestamp,
        isStale=is_stale,
    )
