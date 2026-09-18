"""
The verification stage — Passage 1 §6, with Passage 4 §4's worked
example (G06d) applied literally:

  "before a TradeAnalysis is allowed to reach the user, a final pass
   checks that entry/SL/targets are internally consistent (e.g., a LONG
   setup's stop-loss must be below entry, not above) and that every
   numeric field traces back to a value the engine actually computed. If
   it can't verify a field, the field is null and status becomes
   NO_VALID_SETUP, never a fabricated number — from any of the three
   providers."

Every number in the response is this module's responsibility. The
provider proposes only a direction and a confidence.

What changed this pass: the entry/stop/target arithmetic that used to be
inlined here now comes from `engine/risk_engine.py`, which fixes a
structural defect. The old code placed the stop 1.5 x ATR from entry and
target-1 also 1.5 x ATR from entry, so risk equalled reward by
construction and `riskReward` was the constant 1.0 on every response the
system had ever produced (verified across ten symbols before the
rewrite: every one returned exactly 1.0, and the risk/reward-quality
comparison axis was therefore a constant 50). Targets are now
R-multiples of the actual risk distance, so R:R varies and means
something, and the minimum-R:R and max-risk-per-trade gates can actually
reject a setup.
"""

from __future__ import annotations

import re

from .config import RiskSettings
from .engine import risk_engine
from .observability import get_logger
from .providers.base import ProviderReasoning
from .schemas import (
    IndicatorBundle,
    PositionSizeInfo,
    RegimeInfo,
    RiskInfo,
    StructureInfo,
    TradeAnalysis,
    now_iso,
)
from .utils import round2

logger = get_logger("verify")

# Passage 1 §2: "no 'guaranteed profit' language anywhere in the
# product". Checked on provider prose at the output-validation boundary,
# because a hosted model's wording is not under our control.
_PROHIBITED_CLAIM_PATTERNS = [
    re.compile(r"\bguarantee(?:d|s)?\b", re.IGNORECASE),
    re.compile(r"\brisk[- ]free\b", re.IGNORECASE),
    re.compile(r"\bsure[- ]shot\b", re.IGNORECASE),
    re.compile(r"\bassured returns?\b", re.IGNORECASE),
    re.compile(r"\bcannot lose\b", re.IGNORECASE),
    re.compile(r"\bwill definitely (?:rise|fall|profit)\b", re.IGNORECASE),
]


def _structural_target_level(
    direction: str | None, structure: StructureInfo | None
) -> float | None:
    """
    The market-structure level, if any, that should be considered as
    target 1 — Passage 4 §4's market-structure detection feeding the
    Risk Engine's target selection rather than the two staying
    disconnected. A LONG looks at resistance overhead; a SHORT looks at
    support below.
    """
    if structure is None or direction is None:
        return None
    if direction == "LONG":
        return structure.nearestResistance
    return structure.nearestSupport


def screen_compliance(text: str) -> list[str]:
    """Return a warning per prohibited-claim pattern found."""
    warnings: list[str] = []
    for pattern in _PROHIBITED_CLAIM_PATTERNS:
        match = pattern.search(text or "")
        if match:
            warnings.append(
                f"Provider text contained a prohibited performance claim "
                f"({match.group(0)!r}); surfaced as a warning rather than suppressed."
            )
    return warnings


def _no_setup(
    *,
    indicators: IndicatorBundle,
    reasoning: ProviderReasoning,
    source: str,
    provider_id: str,
    model_id: str,
    persisted: bool,
    data_timestamp: str,
    is_stale: bool,
    regime: RegimeInfo | None,
    structure: StructureInfo | None,
    risk: RiskInfo | None,
    transport: str | None,
    reason: str | None = None,
    warnings: list[str] | None = None,
    knowledge_sources: list[str] | None = None,
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
        contradictingEvidence=reasoning.contradicting_evidence,
        source=source,  # type: ignore[arg-type]
        provider=provider_id,
        model=model_id,
        indicatorsUsed=indicators,
        generatedAt=now_iso(),
        persisted=persisted,
        dataTimestamp=data_timestamp,
        isStale=is_stale,
        regime=regime,
        structure=structure,
        risk=risk,
        transport=transport,  # type: ignore[arg-type]
        validationWarnings=warnings or [],
        knowledgeSources=knowledge_sources or [],
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
    *,
    risk_settings: RiskSettings | None = None,
    regime: RegimeInfo | None = None,
    structure: StructureInfo | None = None,
    transport: str | None = None,
    knowledge_sources: list[str] | None = None,
    engine_warnings: list[str] | None = None,
) -> TradeAnalysis:
    """
    Turn a provider's proposed direction into a fully verified
    TradeAnalysis, or into an explicit NO_VALID_SETUP.

    Positional parameters are unchanged from before this pass, so every
    existing call site keeps working; everything new is keyword-only
    with a default.
    """
    settings = risk_settings or RiskSettings()
    warnings = list(engine_warnings or [])
    warnings.extend(screen_compliance(reasoning.reasoning_summary))

    direction = reasoning.direction
    confidence = reasoning.confidence
    structural_level = _structural_target_level(direction, structure)

    common = dict(
        indicators=indicators,
        reasoning=reasoning,
        source=source,
        provider_id=provider_id,
        model_id=model_id,
        persisted=persisted,
        data_timestamp=data_timestamp,
        is_stale=is_stale,
        regime=regime,
        structure=structure,
        transport=transport,
        knowledge_sources=knowledge_sources,
    )

    # The provider declined, or ATR is unavailable so no volatility-based
    # stop can be placed. Either way there is no verifiable setup.
    if not direction or indicators.atr14 is None or indicators.atr14 <= 0:
        reason = None
        if direction and (indicators.atr14 is None or indicators.atr14 <= 0):
            reason = (
                "A direction was proposed, but ATR(14) is unavailable, so no "
                "volatility-based stop can be verified. Reporting no valid setup rather "
                "than a fabricated stop level."
            )
        return _no_setup(**common, risk=None, reason=reason, warnings=warnings)  # type: ignore[arg-type]

    entry = indicators.lastClose
    assessment = risk_engine.assess(
        direction=direction,
        entry=entry,
        atr=indicators.atr14,
        settings=settings,
        structural_level=structural_level,
    )

    risk_info = RiskInfo(
        riskPerUnit=round2(assessment.risk_per_unit),
        rewardPerUnit=round2(assessment.reward_per_unit),
        riskReward=round2(assessment.risk_reward),
        minRiskReward=settings.min_risk_reward,
        maxRiskPerTradePct=settings.max_risk_per_trade_pct,
        passesMinRiskReward=assessment.passes_min_rr,
        passesMaxRisk=assessment.passes_max_risk,
        targetBasis=assessment.target_basis,
        position=PositionSizeInfo(
            quantity=assessment.position.quantity,
            notional=round2(assessment.position.notional),
            riskAmount=round2(assessment.position.risk_amount),
            riskPctOfEquity=round(assessment.position.risk_pct_of_equity, 4),
            method=assessment.position.method,
            rejectedReason=assessment.position.rejected_reason,
        ),
        rejectedReason=assessment.rejected_reason,
    )

    # Verification: a LONG's stop must sit below entry, a SHORT's above.
    # Reaching this branch would mean the risk engine itself is wrong, so
    # it is checked rather than assumed.
    consistent = (
        assessment.stop_loss < entry if direction == "LONG" else assessment.stop_loss > entry
    )
    if not consistent:
        logger.error(
            "consistency check failed on a computed setup",
            extra={"provider": provider_id, "direction": direction, "entry": entry},
        )
        return _no_setup(
            **common,  # type: ignore[arg-type]
            risk=risk_info,
            reason=(
                "Internal consistency check failed on the computed setup "
                f"(a {direction} stop-loss must sit on the opposite side of entry) — "
                "suppressed rather than returned."
            ),
            warnings=warnings,
        )

    # Targets must also sit on the correct side of entry.
    bad_targets = [
        t
        for t in assessment.targets
        if (direction == "LONG" and t <= entry) or (direction == "SHORT" and t >= entry)
    ]
    if bad_targets:
        return _no_setup(
            **common,  # type: ignore[arg-type]
            risk=risk_info,
            reason=(
                "Computed targets did not all sit on the profitable side of entry — "
                "suppressed rather than returned."
            ),
            warnings=warnings,
        )

    # Risk gates. Declining here is a correct outcome, not a failure.
    if not assessment.is_tradeable:
        return _no_setup(
            **common,  # type: ignore[arg-type]
            risk=risk_info,
            reason=(
                f"{assessment.rejected_reason}. The direction the provider proposed may "
                "still be sound, but the setup does not clear the configured risk rules, "
                "so it is not returned as a tradeable setup."
            ),
            warnings=warnings,
        )

    if confidence is not None and not 0 <= confidence <= 100:
        warnings.append(
            f"Provider returned an out-of-range confidence ({confidence}); clamped."
        )
        confidence = max(0.0, min(100.0, confidence))

    return TradeAnalysis(
        status="SETUP_FOUND",
        direction=direction,
        entry=round2(entry),
        stopLoss=round2(assessment.stop_loss),
        targets=[round2(t) for t in assessment.targets],
        confidence=confidence,
        riskReward=round2(assessment.risk_reward),
        reasoningSummary=reasoning.reasoning_summary,
        supportingEvidence=reasoning.supporting_evidence,
        contradictingEvidence=reasoning.contradicting_evidence,
        source=source,  # type: ignore[arg-type]
        provider=provider_id,
        model=model_id,
        indicatorsUsed=indicators,
        generatedAt=now_iso(),
        persisted=persisted,
        dataTimestamp=data_timestamp,
        isStale=is_stale,
        regime=regime,
        structure=structure,
        risk=risk_info,
        transport=transport,  # type: ignore[arg-type]
        validationWarnings=warnings,
        knowledgeSources=knowledge_sources or [],
    )