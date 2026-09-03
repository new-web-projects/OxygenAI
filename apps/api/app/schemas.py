"""
Pydantic schemas — the request/response contract this API validates
against, at runtime, exactly the way lib/types.ts's Zod schemas did on
the Next.js side. Same shapes, ported field-for-field so the frontend's
existing fetch/render code needs no contract changes, only a new URL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field


class OHLCVBar(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class IndicatorBundle(BaseModel):
    sma20: Optional[float]
    sma50: Optional[float]
    rsi14: Optional[float]
    atr14: Optional[float]
    lastClose: float
    trend: Literal["up", "down", "flat"]


class TradeAnalysis(BaseModel):
    status: Literal["SETUP_FOUND", "NO_VALID_SETUP"]
    direction: Optional[Literal["LONG", "SHORT"]]
    entry: Optional[float]
    stopLoss: Optional[float]
    targets: list[float]
    confidence: Optional[float] = Field(default=None, ge=0, le=100)
    riskReward: Optional[float]
    reasoningSummary: str
    supportingEvidence: list[str]
    source: Literal["mock", "hosted_api", "local_model"]
    provider: str
    model: str
    indicatorsUsed: IndicatorBundle
    generatedAt: str
    persisted: bool
    # Passage 4 §3.6's "Data freshness" field: the underlying market
    # data's own timestamp (the last bar used), plus a stale/live flag —
    # distinct from generatedAt, which is when the analysis was computed,
    # not when the price data itself is as-of.
    dataTimestamp: str
    isStale: bool
    # Passage 4 §3.6's "Market data used" field also names timeframe —
    # a fixed literal for now since "1d" is the only timeframe this
    # system computes anywhere; typed this way (not a bare str) so
    # adding a second timeframe later is a type-checked change, not a
    # silent one.
    timeframe: Literal["1d"] = "1d"


class ScoreBreakdown(BaseModel):
    dataCompleteness: Optional[float]
    indicatorAgreement: Optional[float]
    riskRewardQuality: Optional[float]
    ruleCompliance: Optional[float]
    explanationConsistency: Optional[float]
    historicalValidation: Literal["not_enough_data"] = "not_enough_data"
    predictionOutcome: Literal["not_enough_data"] = "not_enough_data"
    confidenceCalibration: Literal["not_enough_data"] = "not_enough_data"


class ComparisonSlotOk(BaseModel):
    outcome: Literal["ok"] = "ok"
    providerId: str
    analysis: TradeAnalysis
    scores: ScoreBreakdown


class ComparisonSlotUnavailable(BaseModel):
    outcome: Literal["unavailable"] = "unavailable"
    providerId: str
    reason: str


ComparisonSlot = Union[ComparisonSlotOk, ComparisonSlotUnavailable]


class ComparisonResponse(BaseModel):
    mode: Literal["multi"] = "multi"
    symbol: str
    results: list[ComparisonSlot]
    persisted: bool
    generatedAt: str
    # The ai_comparisons row's id, when persisted — the frontend needs
    # this to submit a rating or a preferred-provider choice against the
    # comparison later (Passage 4 §3.6's remaining footer actions; not
    # implemented this pass, but the response needs to carry the id now
    # so that step doesn't require touching this contract again).
    comparisonId: Optional[str] = None


class AnalyzeRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    provider: Optional[str] = None
    providers: Optional[list[str]] = Field(default=None, min_length=2, max_length=3)


class RateComparisonRequest(BaseModel):
    """
    Passage 4 §3.6's two mutating footer actions, on one endpoint since
    both write to the same ai_comparisons row: User rating (1-5, shared
    across the whole comparison) and Select preferred result (per
    column — the id of whichever provider the user picked). Either can
    be sent alone; at least one must be present.
    """

    rating: Optional[int] = Field(default=None, ge=1, le=5)
    preferredProviderId: Optional[str] = None


class ComparisonFeedback(BaseModel):
    comparisonId: str
    providerSet: list[str]
    userChoiceProviderId: Optional[str]
    userRating: Optional[int]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
