import type { AIProvider, AnalysisContext, ProviderReasoning } from "./types";

// Rule-based, not a model call — but it obeys the same law every provider
// must obey: it only ever reasons over indicators.ts's output, and it never
// invents a price. This is what makes it a legitimate fallback rather than
// a fake one.
export const mockProvider: AIProvider = {
  id: "mock",
  displayName: "Mock Reasoner (offline)",
  isConfigured: () => true,
  async reason(context: AnalysisContext): Promise<ProviderReasoning> {
    const { indicators } = context;
    const { trend, rsi14 } = indicators;

    if (trend === "flat" || rsi14 === null) {
      return {
        direction: null,
        confidence: null,
        reasoningSummary:
          "Trend is flat and/or RSI has insufficient history — no directional edge to report.",
        supportingEvidence: [],
      };
    }

    const overbought = rsi14 > 70;
    const oversold = rsi14 < 30;

    if (trend === "up" && !overbought) {
      return {
        direction: "LONG",
        confidence: oversold ? 72 : 58,
        reasoningSummary: `20-SMA is above 50-SMA (uptrend) and RSI(14) at ${rsi14.toFixed(1)} is not overbought, so momentum has room to continue.`,
        supportingEvidence: ["sma20 > sma50", `rsi14 = ${rsi14.toFixed(1)} (not overbought)`],
      };
    }

    if (trend === "down" && !oversold) {
      return {
        direction: "SHORT",
        confidence: overbought ? 70 : 55,
        reasoningSummary: `20-SMA is below 50-SMA (downtrend) and RSI(14) at ${rsi14.toFixed(1)} is not oversold, so downside momentum has room to continue.`,
        supportingEvidence: ["sma20 < sma50", `rsi14 = ${rsi14.toFixed(1)} (not oversold)`],
      };
    }

    return {
      direction: null,
      confidence: null,
      reasoningSummary:
        "Trend direction is already extended on RSI (overbought in an uptrend, or oversold in a downtrend) — no valid setup.",
      supportingEvidence: [],
    };
  },
};
