from app.schemas import IndicatorBundle, TradeAnalysis, now_iso
from app.scoring import score_analysis

BASE_INDICATORS = IndicatorBundle(sma20=100, sma50=98, rsi14=55, atr14=2, lastClose=100, trend="up")


def make_analysis(**overrides) -> TradeAnalysis:
    defaults = dict(
        status="SETUP_FOUND",
        direction="LONG",
        entry=100,
        stopLoss=97,
        targets=[103, 106],
        confidence=60,
        riskReward=1.5,
        reasoningSummary="test",
        supportingEvidence=["sma20 > sma50", "rsi14 = 55 (not overbought)"],
        source="mock",
        provider="mock",
        model="mock-v1",
        indicatorsUsed=BASE_INDICATORS,
        generatedAt=now_iso(),
        persisted=False,
    )
    defaults.update(overrides)
    return TradeAnalysis(**defaults)


def test_no_valid_setup_scores_rule_compliance_100_and_rest_null():
    s = score_analysis(make_analysis(status="NO_VALID_SETUP", direction=None))
    assert s.ruleCompliance == 100
    assert s.dataCompleteness is None
    assert s.historicalValidation == "not_enough_data"


def test_fully_populated_setup_scores_100_data_completeness():
    s = score_analysis(make_analysis())
    assert s.dataCompleteness == 100


def test_citing_a_real_indicator_scores_full_indicator_agreement():
    s = score_analysis(make_analysis(supportingEvidence=["rsi14 = 55"]))
    assert s.indicatorAgreement == 100


def test_citing_no_evidence_scores_zero_indicator_agreement():
    s = score_analysis(make_analysis(supportingEvidence=[]))
    assert s.indicatorAgreement == 0


def test_high_confidence_thin_evidence_scores_low_explanation_consistency():
    s = score_analysis(make_analysis(confidence=90, supportingEvidence=[]))
    assert s.explanationConsistency == 40


def test_history_dependent_axes_are_always_the_placeholder():
    s = score_analysis(make_analysis())
    assert s.historicalValidation == "not_enough_data"
    assert s.predictionOutcome == "not_enough_data"
    assert s.confidenceCalibration == "not_enough_data"
