/**
 * Frontend-only type definitions. Runtime validation (formerly Zod here)
 * now happens once, server-side, in apps/api/app/schemas.py (Pydantic) --
 * the actual API Gateway per Passage 1 §3. These are plain TypeScript
 * types for rendering the response the frontend receives over HTTP; they
 * describe the same contract, they don't re-validate it.
 */

export interface IndicatorBundle {
  sma20: number | null;
  sma50: number | null;
  rsi14: number | null;
  atr14: number | null;
  lastClose: number;
  trend: "up" | "down" | "flat";
}

export interface TradeAnalysis {
  status: "SETUP_FOUND" | "NO_VALID_SETUP";
  direction: "LONG" | "SHORT" | null;
  entry: number | null;
  stopLoss: number | null;
  targets: number[];
  confidence: number | null;
  riskReward: number | null;
  reasoningSummary: string;
  supportingEvidence: string[];
  source: "mock" | "hosted_api" | "local_model";
  provider: string;
  model: string;
  indicatorsUsed: IndicatorBundle;
  generatedAt: string;
  persisted: boolean;
  dataTimestamp: string;
  isStale: boolean;
  timeframe: "1d";
}

export interface ScoreBreakdown {
  dataCompleteness: number | null;
  indicatorAgreement: number | null;
  riskRewardQuality: number | null;
  ruleCompliance: number | null;
  explanationConsistency: number | null;
  historicalValidation: "not_enough_data";
  predictionOutcome: "not_enough_data";
  confidenceCalibration: "not_enough_data";
}

export type ComparisonSlot =
  | { outcome: "ok"; providerId: string; analysis: TradeAnalysis; scores: ScoreBreakdown }
  | { outcome: "unavailable"; providerId: string; reason: string };

export interface ComparisonResponse {
  mode: "multi";
  symbol: string;
  results: ComparisonSlot[];
  persisted: boolean;
  generatedAt: string;
  comparisonId: string | null;
}

// Passage 4 §3.6's mutating footer actions — User rating and Select
// preferred result — both PATCH /api/ai/comparisons/{id}.
export interface ComparisonFeedback {
  comparisonId: string;
  providerSet: string[];
  userChoiceProviderId: string | null;
  userRating: number | null;
}
