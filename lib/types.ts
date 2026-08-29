import { z } from "zod";

export const OHLCVBarSchema = z.object({
  timestamp: z.string(),
  open: z.number(),
  high: z.number(),
  low: z.number(),
  close: z.number(),
  volume: z.number(),
});
export type OHLCVBar = z.infer<typeof OHLCVBarSchema>;

export const IndicatorBundleSchema = z.object({
  sma20: z.number().nullable(),
  sma50: z.number().nullable(),
  rsi14: z.number().nullable(),
  atr14: z.number().nullable(),
  lastClose: z.number(),
  trend: z.enum(["up", "down", "flat"]),
});
export type IndicatorBundle = z.infer<typeof IndicatorBundleSchema>;

// The one object every provider must produce, and the one object the API
// route ever returns. Mirrors the blueprint's TradeAnalysis contract:
// schema-validated, allowed to legitimately say NO_VALID_SETUP, and always
// carries a source tag so the caller knows whether a real model produced it.
export const TradeAnalysisSchema = z.object({
  status: z.enum(["SETUP_FOUND", "NO_VALID_SETUP"]),
  direction: z.enum(["LONG", "SHORT"]).nullable(),
  entry: z.number().nullable(),
  stopLoss: z.number().nullable(),
  targets: z.array(z.number()),
  confidence: z.number().min(0).max(100).nullable(),
  riskReward: z.number().nullable(),
  reasoningSummary: z.string(),
  supportingEvidence: z.array(z.string()),
  source: z.enum(["mock", "hosted_api", "local_model"]),
  provider: z.string(),
  model: z.string(),
  indicatorsUsed: IndicatorBundleSchema,
  generatedAt: z.string(),
  // Whether this request actually read/wrote the database (DATABASE_URL
  // configured and reachable) or ran on ephemeral synthetic data.
  persisted: z.boolean(),
});
export type TradeAnalysis = z.infer<typeof TradeAnalysisSchema>;
