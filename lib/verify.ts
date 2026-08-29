import type { IndicatorBundle, TradeAnalysis } from "./types";
import type { ProviderReasoning } from "./providers/types";

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

function noSetup(
  indicators: IndicatorBundle,
  reasoning: ProviderReasoning,
  source: TradeAnalysis["source"],
  providerId: string,
  modelId: string,
  reason?: string
): TradeAnalysis {
  return {
    status: "NO_VALID_SETUP",
    direction: null,
    entry: null,
    stopLoss: null,
    targets: [],
    confidence: null,
    riskReward: null,
    reasoningSummary: reason ?? reasoning.reasoningSummary,
    supportingEvidence: reason ? [] : reasoning.supportingEvidence,
    source,
    provider: providerId,
    model: modelId,
    indicatorsUsed: indicators,
    generatedAt: new Date().toISOString(),
  };
}

/**
 * The verification stage. Entry/stop/targets are computed HERE, from ATR —
 * never taken from the AI provider's own text. The provider only gets to
 * propose a direction and a confidence; every number in the response is
 * this function's responsibility. A consistency check runs before anything
 * is returned; if it fails, the setup is suppressed rather than shipped.
 */
export function buildTradeAnalysis(
  indicators: IndicatorBundle,
  reasoning: ProviderReasoning,
  source: TradeAnalysis["source"],
  providerId: string,
  modelId: string
): TradeAnalysis {
  const { direction, confidence } = reasoning;

  if (!direction || indicators.atr14 === null) {
    return noSetup(indicators, reasoning, source, providerId, modelId);
  }

  const entry = indicators.lastClose;
  const atr = indicators.atr14;
  const stopLoss = direction === "LONG" ? entry - 1.5 * atr : entry + 1.5 * atr;
  const target1 = direction === "LONG" ? entry + 1.5 * atr : entry - 1.5 * atr;
  const target2 = direction === "LONG" ? entry + 3 * atr : entry - 3 * atr;

  const consistent = direction === "LONG" ? stopLoss < entry : stopLoss > entry;
  if (!consistent) {
    return noSetup(
      indicators,
      reasoning,
      source,
      providerId,
      modelId,
      "Internal consistency check failed on the computed setup — suppressed rather than returned."
    );
  }

  const risk = Math.abs(entry - stopLoss);
  const reward = Math.abs(target1 - entry);
  const riskReward = risk > 0 ? round2(reward / risk) : null;

  return {
    status: "SETUP_FOUND",
    direction,
    entry: round2(entry),
    stopLoss: round2(stopLoss),
    targets: [round2(target1), round2(target2)],
    confidence,
    riskReward,
    reasoningSummary: reasoning.reasoningSummary,
    supportingEvidence: reasoning.supportingEvidence,
    source,
    provider: providerId,
    model: modelId,
    indicatorsUsed: indicators,
    generatedAt: new Date().toISOString(),
  };
}
