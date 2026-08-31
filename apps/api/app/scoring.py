"""
Port of lib/scoring.ts. Passage 4 §3.4's exact 8 scoring axes. The last
three need outcome history from trade_results that doesn't exist in this
demo (no paper-trading loop is built) — rendered as an explicit "not
enough data yet" placeholder. That placeholder behavior is itself a real,
specified requirement (Passage 4 G10), not a stand-in for the missing
feature.
"""

from __future__ import annotations

from .schemas import ScoreBreakdown, TradeAnalysis

_INDICATOR_NAMES = ["sma20", "sma50", "rsi14", "atr14", "sma", "rsi", "atr"]


def score_analysis(analysis: TradeAnalysis) -> ScoreBreakdown:
    if analysis.status == "NO_VALID_SETUP":
        return ScoreBreakdown(
            dataCompleteness=None,
            indicatorAgreement=None,
            riskRewardQuality=None,
            # Correctly declining a bad setup is compliant behavior, not
            # a failure to score down.
            ruleCompliance=100,
            explanationConsistency=None,
        )

    required_fields = [
        analysis.entry,
        analysis.stopLoss,
        analysis.targets[0] if analysis.targets else None,
        analysis.confidence,
        analysis.riskReward,
    ]
    present = sum(1 for f in required_fields if f is not None)
    data_completeness = round((present / len(required_fields)) * 100)

    # Simplified proxy for "overlap between cited evidence and the actual
    # computed indicators" (Passage 4's definition): checks whether the
    # provider named a real indicator field, not whether the cited value
    # is numerically correct.
    cites_real = any(
        name in evidence.lower() for evidence in analysis.supportingEvidence for name in _INDICATOR_NAMES
    )
    if not analysis.supportingEvidence:
        indicator_agreement: float | None = 0
    else:
        indicator_agreement = 100 if cites_real else 40

    risk_reward_quality = (
        max(0, min(100, round(analysis.riskReward * 50))) if analysis.riskReward is not None else None
    )

    # Reaching this point already means build_trade_analysis's
    # consistency check passed — a failed one never reaches SETUP_FOUND.
    rule_compliance = 100

    evidence_count = len(analysis.supportingEvidence)
    confidence = analysis.confidence or 0
    # The pattern this axis exists to catch: high stated confidence with
    # little evidence behind it.
    if confidence > 60 and evidence_count < 2:
        explanation_consistency = 40
    elif confidence <= 60 and evidence_count == 0:
        explanation_consistency = 70
    else:
        explanation_consistency = 90

    return ScoreBreakdown(
        dataCompleteness=data_completeness,
        indicatorAgreement=indicator_agreement,
        riskRewardQuality=risk_reward_quality,
        ruleCompliance=rule_compliance,
        explanationConsistency=explanation_consistency,
    )
