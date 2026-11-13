"""
The request/response contract, validated at runtime by Pydantic.

Extended this pass, strictly additively — every field that existed
before still exists with the same name, type and meaning, so the
frontend's current rendering keeps working while the new surfaces
(admin panel, error center, charting) get what they need. New fields are
optional with defaults for the same reason.

Field naming stays camelCase to match the TypeScript consumer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

# JSON has no NaN or Infinity. A float field typed this way rejects them at
# the model boundary with a precise validation error instead of surfacing
# later as an opaque "Out of range float values are not JSON compliant"
# failure inside the response encoder. Used for values derived from
# external computation (e.g. vector-similarity scores).
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]

# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


class OHLCVBar(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


# ---------------------------------------------------------------------------
# Deterministic engine output
# ---------------------------------------------------------------------------


class IndicatorBundle(BaseModel):
    """
    Passage 4 §4 (G06) names the required set: RSI, MACD, Stochastic,
    ATR, ADX, Bollinger Bands, VWAP, SMA/EMA. The first four fields
    predate this pass; the rest are new.

    Every new field is Optional and defaults to None so a short history
    degrades field-by-field rather than failing the whole bundle —
    Passage 1 §6's "null field, not a fabricated number".
    """

    sma20: Optional[float]
    sma50: Optional[float]
    rsi14: Optional[float]
    atr14: Optional[float]
    lastClose: float
    trend: Literal["up", "down", "flat"]

    ema20: Optional[float] = None
    ema50: Optional[float] = None
    macd: Optional[float] = None
    macdSignal: Optional[float] = None
    macdHistogram: Optional[float] = None
    stochasticK: Optional[float] = None
    stochasticD: Optional[float] = None
    adx: Optional[float] = None
    plusDi: Optional[float] = None
    minusDi: Optional[float] = None
    bollingerUpper: Optional[float] = None
    bollingerMiddle: Optional[float] = None
    bollingerLower: Optional[float] = None
    bollingerPercentB: Optional[float] = None
    vwap: Optional[float] = None


class RegimeInfo(BaseModel):
    """Passage 4 §4 (G06b) — the regime classifier's output."""

    regime: Literal["trending_up", "trending_down", "ranging", "high_volatility", "unknown"]
    adx: Optional[float] = None
    atrPercentile: Optional[float] = None
    relevantSignalFamilies: list[str] = Field(default_factory=list)
    rationale: str = ""


class BreakoutInfo(BaseModel):
    direction: Literal["up", "down"]
    level: float
    close: float
    volumeRatio: float
    confirmed: bool
    reason: str


class LevelInfo(BaseModel):
    price: float
    kind: Literal["support", "resistance"]
    touches: int
    strength: float


class StructureInfo(BaseModel):
    """Passage 4 §4 (G06) — market structure / S-R / breakout detection."""

    structureBias: Literal["higher_highs", "lower_lows", "sideways", "unknown"] = "unknown"
    nearestSupport: Optional[float] = None
    nearestResistance: Optional[float] = None
    support: list[LevelInfo] = Field(default_factory=list)
    resistance: list[LevelInfo] = Field(default_factory=list)
    breakout: Optional[BreakoutInfo] = None


class PositionSizeInfo(BaseModel):
    """Passage 4 §4 (G06) Risk Engine — position sizing and max-risk enforcement."""

    quantity: int
    notional: float
    riskAmount: float
    riskPctOfEquity: float
    method: str
    rejectedReason: Optional[str] = None


class RiskInfo(BaseModel):
    riskPerUnit: float
    rewardPerUnit: float
    riskReward: float
    minRiskReward: float
    maxRiskPerTradePct: float
    passesMinRiskReward: bool
    passesMaxRisk: bool
    targetBasis: str = "fixed R-multiples"
    position: Optional[PositionSizeInfo] = None
    rejectedReason: Optional[str] = None


# ---------------------------------------------------------------------------
# TradeAnalysis — the schema-validated object every provider produces
# ---------------------------------------------------------------------------


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
    source: Literal["mock", "hosted_api", "local_model", "cache", "fallback"]
    provider: str
    model: str
    indicatorsUsed: IndicatorBundle
    generatedAt: str
    persisted: bool
    dataTimestamp: str
    isStale: bool
    timeframe: Literal["1d"] = "1d"

    # New this pass.
    # Passage 4 §3.6's comparison-UI field list pairs supporting with
    # contradicting evidence; only the supporting half existed.
    contradictingEvidence: list[str] = Field(default_factory=list)
    regime: Optional[RegimeInfo] = None
    structure: Optional[StructureInfo] = None
    risk: Optional[RiskInfo] = None
    # Passage 1 §7: which labelled sources fed the reasoning.
    knowledgeSources: list[str] = Field(default_factory=list)
    # Passage 4 §3.6: the active-model badge distinguishes hosted from
    # local-runtime responses per column.
    transport: Optional[Literal["hosted_api", "local_model", "mock", "cache", "fallback"]] = None
    # Set (single-provider mode) when a different provider answered than the one the caller
    # requested — e.g. "grok" requested, "mock" answered because Grok is not configured or failed.
    # `provider`/`source` already said so, but only for a client that knew to compare them.
    requestedProvider: Optional[str] = None
    validationWarnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


class ScoreBreakdown(BaseModel):
    dataCompleteness: Optional[float]
    indicatorAgreement: Optional[float]
    riskRewardQuality: Optional[float]
    ruleCompliance: Optional[float]
    explanationConsistency: Optional[float]
    historicalValidation: Union[float, Literal["not_enough_data"]] = "not_enough_data"
    predictionOutcome: Union[float, Literal["not_enough_data"]] = "not_enough_data"
    confidenceCalibration: Union[float, Literal["not_enough_data"]] = "not_enough_data"


class ComparisonSlotOk(BaseModel):
    outcome: Literal["ok"] = "ok"
    providerId: str
    analysis: TradeAnalysis
    scores: ScoreBreakdown
    latencyMs: Optional[float] = None


class ComparisonSlotUnavailable(BaseModel):
    outcome: Literal["unavailable"] = "unavailable"
    providerId: str
    reason: str
    # Distinguishes "no key configured" from "circuit open" from "the
    # call failed" — the UI shows different affordances for each.
    reasonCode: Literal[
        "not_configured", "circuit_open", "call_failed", "unknown_provider", "disabled", "no_model"
    ] = "call_failed"
    retryAfterSeconds: Optional[float] = None


ComparisonSlot = Union[ComparisonSlotOk, ComparisonSlotUnavailable]


class ComparisonResponse(BaseModel):
    mode: Literal["multi"] = "multi"
    symbol: str
    results: list[ComparisonSlot]
    persisted: bool
    generatedAt: str
    comparisonId: Optional[str] = None
    # Passage 4 §3.3 (G11): concurrent execution means total latency is
    # ~max(participants), not the sum. Both are reported so the property
    # is observable rather than only asserted.
    totalLatencyMs: Optional[float] = None
    maxProviderLatencyMs: Optional[float] = None
    sumProviderLatencyMs: Optional[float] = None


class AnalyzeRequest(BaseModel):
    """
    Passage 1 §10 specifies a `mode` field of
    "custom"|"grok"|"gemma"|"multi", with `providers` required when
    mode is "multi".

    The pre-existing shape used `provider` / `providers` instead. Both
    are accepted: `mode` is the blueprint's contract, `provider` is kept
    so the existing frontend and any existing caller keep working
    unchanged. `normalized_providers()` reconciles them.
    """

    symbol: str = Field(min_length=1, max_length=20)
    mode: Optional[Literal["custom", "grok", "gemma", "multi", "mock"]] = None
    provider: Optional[str] = None
    providers: Optional[list[str]] = Field(default=None, min_length=2, max_length=3)
    timeframe: Literal["1d"] = "1d"
    # Optional per-request risk overrides; fall back to configured
    # defaults when absent.
    accountEquity: Optional[float] = Field(default=None, gt=0)
    maxRiskPerTradePct: Optional[float] = Field(default=None, gt=0, le=100)
    useKnowledge: bool = True

    def is_multi(self) -> bool:
        return self.mode == "multi" or bool(self.providers)

    def single_provider_id(self) -> str:
        if self.provider:
            return self.provider
        if self.mode and self.mode != "multi":
            return self.mode
        return "mock"


class RouteRequest(BaseModel):
    """Passage 4 §6.1 (G08a) — POST /api/ai/route."""

    mode: Optional[Literal["custom", "grok", "gemma", "multi", "mock"]] = None
    provider: Optional[str] = None
    providers: Optional[list[str]] = Field(default=None, min_length=2, max_length=3)
    allowFallback: bool = True


class RouteDecisionResponse(BaseModel):
    requestedProviderId: str
    resolvedProviderId: str
    modelId: str
    sourceTag: str
    available: bool
    reason: str
    circuitState: str
    fellBack: bool
    missingConfiguration: list[str] = Field(default_factory=list)
    modelRedirectNote: Optional[str] = None


class RouteResponse(BaseModel):
    mode: str
    decisions: list[RouteDecisionResponse]


# ---------------------------------------------------------------------------
# Comparison feedback (pre-existing, unchanged)
# ---------------------------------------------------------------------------


class RateComparisonRequest(BaseModel):
    """
    Passage 4 §3.6's two mutating footer actions on one endpoint, since
    both write the same ai_comparisons row: User rating (1-5) and Select
    preferred result. Either can be sent alone; at least one must be.
    """

    rating: Optional[int] = Field(default=None, ge=1, le=5)
    preferredProviderId: Optional[str] = None


class ComparisonFeedback(BaseModel):
    comparisonId: str
    providerSet: list[str]
    userChoiceProviderId: Optional[str]
    userRating: Optional[int]


# ---------------------------------------------------------------------------
# Provider / model admin surfaces
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    ok: bool
    latencyMs: Optional[float] = None
    modelChecked: Optional[str] = None
    # Passage 4 §6.2 writes the contract as `{ ok, latency_ms, model_checked }`. The camelCase
    # pair is what this API's other responses use; the snake_case pair is the specified shape.
    # Both are always present and always equal.
    latency_ms: Optional[float] = None
    model_checked: Optional[str] = None
    detail: str
    transport: str = "none"

    @model_validator(mode="after")
    def _mirror_snake_case(self) -> "HealthResponse":
        self.latency_ms = self.latencyMs
        self.model_checked = self.modelChecked
        return self


class ModelCreateRequest(BaseModel):
    providerId: str
    modelId: str = Field(min_length=1, max_length=120)
    displayName: str = Field(min_length=1, max_length=200)
    status: Literal[
        "active", "disabled", "deprecated", "retired", "local", "hosted", "fallback"
    ] = "active"
    contextWindow: Optional[int] = Field(default=None, ge=0)
    inputModalities: Optional[list[str]] = Field(default=None, max_length=10)
    outputModalities: Optional[list[str]] = Field(default=None, max_length=10)
    isDefault: bool = False
    isFallback: bool = False
    onlineCapable: bool = True
    offlineCapable: bool = False
    config: dict[str, Any] = Field(default_factory=dict)


class ModelUpdateRequest(BaseModel):
    displayName: Optional[str] = Field(default=None, min_length=1, max_length=200)
    status: Optional[
        Literal["active", "disabled", "deprecated", "retired", "local", "hosted", "fallback"]
    ] = None
    contextWindow: Optional[int] = Field(default=None, ge=0)
    inputModalities: Optional[list[str]] = Field(default=None, max_length=10)
    outputModalities: Optional[list[str]] = Field(default=None, max_length=10)
    isDefault: Optional[bool] = None
    isFallback: Optional[bool] = None
    onlineCapable: Optional[bool] = None
    offlineCapable: Optional[bool] = None
    config: Optional[dict[str, Any]] = None


class ModelResponse(BaseModel):
    id: str
    providerId: str
    modelId: str
    displayName: str
    status: Literal["active", "disabled", "deprecated", "retired", "local", "hosted", "fallback"]
    contextWindow: Optional[int] = None
    inputModalities: list[str]
    outputModalities: list[str]
    isDefault: bool
    isFallback: bool
    onlineCapable: bool
    offlineCapable: bool
    config: dict[str, Any]
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None


class ProviderPatchRequest(BaseModel):
    """PATCH /api/ai/providers/{id} — administrative flags on `ai_providers`."""

    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=0, le=1000)


class ProviderSettingsPatchRequest(BaseModel):
    """
    PATCH /api/ai/providers/{id}/settings. A field that is ABSENT is left alone; an explicit JSON
    `null` clears the override (the environment / default applies again). `apiKey` is write-only.
    """

    model_config = ConfigDict(extra="forbid")
    apiKey: Optional[str] = Field(default=None, min_length=8, max_length=4096)
    baseUrl: Optional[str] = Field(default=None, max_length=500)
    timeoutMs: Optional[int] = Field(default=None, ge=100, le=300_000)
    retryCount: Optional[int] = Field(default=None, ge=0, le=10)
    region: Optional[str] = Field(default=None, max_length=64)


class BreakerLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    failureThreshold: Optional[int] = Field(default=None, ge=1, le=100)
    windowSeconds: Optional[float] = Field(default=None, ge=1, le=3600)
    cooldownSeconds: Optional[float] = Field(default=None, ge=1, le=3600)


class BreakerSettingsRequest(BreakerLimits):
    """
    PUT /api/ai/circuit-breaker/settings — replaces the stored thresholds. Anything omitted falls
    back to the environment default (CB_*), so `{}` means "defaults". `providers` overrides single
    providers (Passage 4 §3.3: "admin-configurable ... applies identically across all three
    providers"; per-provider values are an addition for an unreliable vendor).
    """

    providers: Optional[dict[str, BreakerLimits]] = None


class CompareRequest(BaseModel):
    """
    POST /api/ai/compare — Passage 1 §10: "Updated to accept 2 or 3 provider_id values". Same
    engine, providers and persistence as `POST /api/ai/analyze` with `mode: "multi"`.
    """

    model_config = ConfigDict(populate_by_name=True)
    symbol: str = Field(min_length=1, max_length=20)
    providers: list[str] = Field(
        min_length=2,
        max_length=3,
        validation_alias=AliasChoices("providers", "provider_ids", "providerIds"),
    )
    timeframe: Literal["1d"] = "1d"
    accountEquity: Optional[float] = Field(default=None, gt=0)
    maxRiskPerTradePct: Optional[float] = Field(default=None, gt=0, le=100)
    useKnowledge: bool = True


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)


class TokenResponse(BaseModel):
    accessToken: str
    tokenType: Literal["bearer"] = "bearer"
    expiresIn: int
    user: "UserResponse"


class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    status: str


# ---------------------------------------------------------------------------
# Knowledge / RAG
# ---------------------------------------------------------------------------


class KnowledgeUploadRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1)
    sourceType: Literal[
        "knowledge", "live_market_data", "historical_data", "user_provided", "ai_inference"
    ] = "knowledge"


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    topK: int = Field(default=5, ge=1, le=25)


class KnowledgeChunkResponse(BaseModel):
    chunkId: str
    documentId: str
    documentTitle: str
    sourceType: str
    content: str
    score: FiniteFloat
    position: int


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


class MemoryWriteRequest(BaseModel):
    layer: Literal[
        "short_term", "session", "long_term", "knowledge", "trading", "system"
    ]
    key: str = Field(min_length=1, max_length=200)
    value: dict[str, Any]
    ttlSeconds: Optional[int] = Field(default=None, ge=1)


class MemoryEntryResponse(BaseModel):
    layer: str
    key: str
    value: dict[str, Any]
    expiresAt: Optional[str] = None


# ---------------------------------------------------------------------------
# Custom indicators (Passage 4 §3.1 / G01b)
# ---------------------------------------------------------------------------


class CustomIndicatorRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    formula: dict[str, Any]
    status: Literal["draft", "active", "disabled"] = "draft"


class CustomIndicatorTestRequest(BaseModel):
    formula: dict[str, Any]
    symbol: str = Field(min_length=1, max_length=20)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


TokenResponse.model_rebuild()