#!/usr/bin/env bash
# Applies the multi-provider comparison feature.
# Run from the repo root (OxygenAI/): bash apply_comparison_feature.sh
set -euo pipefail

echo "Creating directories..."
mkdir -p lib/db lib/providers app/api/analyze

echo 'Writing (new): lib/comparison.ts'
cat > lib/comparison.ts << 'OXYGEN_AI_FILE_EOF'
import { getProviderStrict } from "./providers";
import { buildTradeAnalysis } from "./verify";
import { scoreAnalysis } from "./scoring";
import type { ComparisonSlot, IndicatorBundle, TradeAnalysis } from "./types";

// mock is not one of the blueprint's authoritative combinations (Custom
// AI / Grok / Gemma 4, any 2 or all 3) — it's kept valid here too, purely
// so the comparison view can be demonstrated without live credentials.
// Comparing a provider against itself isn't a comparison, so it's not
// allowed to appear twice.
const VALID_PROVIDER_IDS = ["mock", "custom", "grok", "gemma"];

export function validateProviderSet(providers: string[]): string | null {
  if (providers.length < 2 || providers.length > 3) {
    return "providers must list 2 or 3 provider ids";
  }
  if (new Set(providers).size !== providers.length) {
    return "providers must not repeat";
  }
  for (const p of providers) {
    if (!VALID_PROVIDER_IDS.includes(p)) {
      return `unknown provider id: ${p} (must be one of ${VALID_PROVIDER_IDS.join(", ")})`;
    }
  }
  return null;
}

/**
 * Runs every requested provider concurrently and isolates failures per
 * slot — this is Passage 1 §4.5's isolation guarantee
 * (asyncio.gather(..., return_exceptions=True)); Promise.allSettled is
 * the direct JS equivalent. One provider being unconfigured or erroring
 * never prevents the others from returning a real result, and the
 * response is always 2-3 slots, never a single hard failure.
 */
export async function runComparison(
  providerIds: string[],
  symbol: string,
  indicators: IndicatorBundle,
  lastBars: { timestamp: string; close: number }[],
  modelIdFor: (providerId: string) => string
): Promise<ComparisonSlot[]> {
  const settled = await Promise.allSettled(
    providerIds.map(async (id): Promise<Extract<ComparisonSlot, { outcome: "ok" }>> => {
      const provider = getProviderStrict(id);
      const reasoning = await provider.reason({ symbol, indicators, lastBars });
      const modelId = modelIdFor(id);
      const source: TradeAnalysis["source"] = id === "mock" ? "mock" : "hosted_api";
      const analysis = buildTradeAnalysis(indicators, reasoning, source, id, modelId, false);
      return { outcome: "ok", providerId: id, analysis, scores: scoreAnalysis(analysis) };
    })
  );

  return settled.map((result, i): ComparisonSlot => {
    if (result.status === "fulfilled") return result.value;
    return {
      outcome: "unavailable",
      providerId: providerIds[i],
      reason: result.reason instanceof Error ? result.reason.message : String(result.reason),
    };
  });
}
OXYGEN_AI_FILE_EOF

echo 'Writing (new): lib/comparison.test.ts'
cat > lib/comparison.test.ts << 'OXYGEN_AI_FILE_EOF'
import { test } from "node:test";
import assert from "node:assert/strict";
import { validateProviderSet, runComparison } from "./comparison";
import { computeIndicators, generateSyntheticOHLCV } from "./indicators";

test("validateProviderSet rejects fewer than 2 providers", () => {
  assert.equal(validateProviderSet(["mock"]), "providers must list 2 or 3 provider ids");
});

test("validateProviderSet rejects more than 3 providers", () => {
  assert.equal(
    validateProviderSet(["mock", "custom", "grok", "gemma"]),
    "providers must list 2 or 3 provider ids"
  );
});

test("validateProviderSet rejects a repeated provider", () => {
  assert.equal(validateProviderSet(["mock", "mock"]), "providers must not repeat");
});

test("validateProviderSet rejects an unknown provider id", () => {
  const err = validateProviderSet(["mock", "chatgpt"]);
  assert.ok(err && err.includes("unknown provider id"));
});

test("validateProviderSet accepts a real 2-provider blueprint combination", () => {
  assert.equal(validateProviderSet(["grok", "gemma"]), null);
});

test("validateProviderSet accepts the full three-way", () => {
  assert.equal(validateProviderSet(["custom", "grok", "gemma"]), null);
});

test("isolation: one unconfigured provider does not prevent the others from succeeding", async () => {
  const bars = generateSyntheticOHLCV("ISOTEST", 60);
  const indicators = computeIndicators(bars);
  const lastBars = bars.slice(-5).map((b) => ({ timestamp: b.timestamp, close: b.close }));

  // grok and gemma have no keys configured in this environment -- both
  // slots should come back "unavailable", not throw and take mock down
  // with them.
  const results = await runComparison(
    ["mock", "grok", "gemma"],
    "ISOTEST",
    indicators,
    lastBars,
    (id) => `${id}-model`
  );

  assert.equal(results.length, 3);

  const mockSlot = results.find((r) => r.providerId === "mock")!;
  assert.equal(mockSlot.outcome, "ok");

  const grokSlot = results.find((r) => r.providerId === "grok")!;
  assert.equal(grokSlot.outcome, "unavailable");

  const gemmaSlot = results.find((r) => r.providerId === "gemma")!;
  assert.equal(gemmaSlot.outcome, "unavailable");
});

test("isolation: two unconfigured providers each get their own independent reason", async () => {
  const bars = generateSyntheticOHLCV("ISOTEST2", 60);
  const indicators = computeIndicators(bars);
  const lastBars = bars.slice(-5).map((b) => ({ timestamp: b.timestamp, close: b.close }));

  const results = await runComparison(["grok", "gemma"], "ISOTEST2", indicators, lastBars, (id) => `${id}-model`);

  assert.equal(results.length, 2);
  for (const r of results) {
    assert.equal(r.outcome, "unavailable");
    if (r.outcome === "unavailable") {
      assert.ok(r.reason.length > 0);
    }
  }
  // different providers, different underlying env vars -- confirms each
  // slot is independently evaluated, not one error copied across both
  const [a, b] = results as { outcome: "unavailable"; providerId: string; reason: string }[];
  assert.notEqual(a.reason, b.reason);
});
OXYGEN_AI_FILE_EOF

echo 'Writing (new): lib/scoring.ts'
cat > lib/scoring.ts << 'OXYGEN_AI_FILE_EOF'
import type { ScoreBreakdown, TradeAnalysis } from "./types";

export type { ScoreBreakdown };

/**
 * Passage 4 §3.4's exact 8 scoring axes. The last three need outcome
 * history from trade_results that doesn't exist in this demo (no
 * paper-trading loop is built) — rendered as an explicit "not enough
 * data yet" placeholder. That placeholder behavior is itself a real,
 * specified requirement (Passage 4 G10: silently omitting these axes
 * would look like a bug, an inconsistent axis count between accounts),
 * not a stand-in for the missing feature.
 */
export function scoreAnalysis(analysis: TradeAnalysis): ScoreBreakdown {
  if (analysis.status === "NO_VALID_SETUP") {
    return {
      dataCompleteness: null,
      indicatorAgreement: null,
      riskRewardQuality: null,
      // Correctly declining a bad setup is compliant behavior, not a
      // failure to score down.
      ruleCompliance: 100,
      explanationConsistency: null,
      historicalValidation: "not_enough_data",
      predictionOutcome: "not_enough_data",
      confidenceCalibration: "not_enough_data",
    };
  }

  const requiredFields = [
    analysis.entry,
    analysis.stopLoss,
    analysis.targets[0],
    analysis.confidence,
    analysis.riskReward,
  ];
  const dataCompleteness = Math.round(
    (requiredFields.filter((f) => f !== null && f !== undefined).length / requiredFields.length) * 100
  );

  // Simplified proxy for "overlap between cited evidence and the actual
  // computed indicators" (Passage 4's definition): checks whether the
  // provider named a real indicator field, not whether the cited value is
  // numerically correct — a fuller version would parse and check the
  // number itself against indicatorsUsed.
  const indicatorNames = ["sma20", "sma50", "rsi14", "atr14", "sma", "rsi", "atr"];
  const citesReal = analysis.supportingEvidence.some((e) =>
    indicatorNames.some((n) => e.toLowerCase().includes(n))
  );
  const indicatorAgreement = analysis.supportingEvidence.length === 0 ? 0 : citesReal ? 100 : 40;

  const riskRewardQuality =
    analysis.riskReward === null ? null : Math.max(0, Math.min(100, Math.round(analysis.riskReward * 50)));

  // Reaching this point already means buildTradeAnalysis's consistency
  // check passed — a failed one never reaches SETUP_FOUND.
  const ruleCompliance = 100;

  const evidenceCount = analysis.supportingEvidence.length;
  const confidence = analysis.confidence ?? 0;
  // The pattern this axis exists to catch: high stated confidence with
  // little evidence behind it.
  const explanationConsistency =
    confidence > 60 && evidenceCount < 2 ? 40 : confidence <= 60 && evidenceCount === 0 ? 70 : 90;

  return {
    dataCompleteness,
    indicatorAgreement,
    riskRewardQuality,
    ruleCompliance,
    explanationConsistency,
    historicalValidation: "not_enough_data",
    predictionOutcome: "not_enough_data",
    confidenceCalibration: "not_enough_data",
  };
}
OXYGEN_AI_FILE_EOF

echo 'Writing (new): lib/scoring.test.ts'
cat > lib/scoring.test.ts << 'OXYGEN_AI_FILE_EOF'
import { test } from "node:test";
import assert from "node:assert/strict";
import { scoreAnalysis } from "./scoring";
import type { TradeAnalysis } from "./types";

const baseIndicators = {
  sma20: 100,
  sma50: 98,
  rsi14: 55,
  atr14: 2,
  lastClose: 100,
  trend: "up" as const,
};

function makeAnalysis(overrides: Partial<TradeAnalysis>): TradeAnalysis {
  return {
    status: "SETUP_FOUND",
    direction: "LONG",
    entry: 100,
    stopLoss: 97,
    targets: [103, 106],
    confidence: 60,
    riskReward: 1.5,
    reasoningSummary: "test",
    supportingEvidence: ["sma20 > sma50", "rsi14 = 55 (not overbought)"],
    source: "mock",
    provider: "mock",
    model: "mock-v1",
    indicatorsUsed: baseIndicators,
    generatedAt: new Date().toISOString(),
    persisted: false,
    ...overrides,
  };
}

test("NO_VALID_SETUP scores ruleCompliance 100 and everything else null/placeholder", () => {
  const s = scoreAnalysis(makeAnalysis({ status: "NO_VALID_SETUP", direction: null }));
  assert.equal(s.ruleCompliance, 100);
  assert.equal(s.dataCompleteness, null);
  assert.equal(s.historicalValidation, "not_enough_data");
});

test("a fully-populated setup scores 100 data completeness", () => {
  const s = scoreAnalysis(makeAnalysis({}));
  assert.equal(s.dataCompleteness, 100);
});

test("citing a real indicator by name scores full indicator agreement", () => {
  const s = scoreAnalysis(makeAnalysis({ supportingEvidence: ["rsi14 = 55"] }));
  assert.equal(s.indicatorAgreement, 100);
});

test("citing no evidence at all scores zero indicator agreement", () => {
  const s = scoreAnalysis(makeAnalysis({ supportingEvidence: [] }));
  assert.equal(s.indicatorAgreement, 0);
});

test("high confidence with thin evidence scores low explanation consistency", () => {
  const s = scoreAnalysis(makeAnalysis({ confidence: 90, supportingEvidence: [] }));
  assert.equal(s.explanationConsistency, 40);
});

test("the three history-dependent axes are always the explicit placeholder, never a number", () => {
  const s = scoreAnalysis(makeAnalysis({}));
  assert.equal(s.historicalValidation, "not_enough_data");
  assert.equal(s.predictionOutcome, "not_enough_data");
  assert.equal(s.confidenceCalibration, "not_enough_data");
});
OXYGEN_AI_FILE_EOF

echo 'Writing (new): lib/db/comparisons.ts'
cat > lib/db/comparisons.ts << 'OXYGEN_AI_FILE_EOF'
import { getPool } from "./client";
import { getOrSeedModel } from "./signals";
import type { ComparisonSlot } from "../types";

/**
 * Persists a completed comparison: one ai_comparisons row (provider_set as
 * jsonb — Passage 1 §9.1's fix for Revision 2's hardcoded two-column
 * design), and one ai_comparison_results row per participating provider
 * (2 or 3 rows), each linked to its seeded ai_providers/ai_models rows and,
 * where one exists, the trading_signals row saveSignal() created.
 */
export async function saveComparison(
  providerIds: string[],
  scores: Record<string, unknown>,
  signalIdByProvider: Record<string, string | null>,
  modelIdByProvider: Record<string, string>
): Promise<string> {
  const pool = getPool();

  const comparisonResult = await pool.query<{ id: string }>(
    `INSERT INTO ai_comparisons (provider_set, scores) VALUES ($1, $2) RETURNING id`,
    [JSON.stringify(providerIds), JSON.stringify(scores)]
  );
  const comparisonId = comparisonResult.rows[0].id;

  for (const providerId of providerIds) {
    const { providerId: providerDbId, modelDbId } = await getOrSeedModel(
      providerId,
      modelIdByProvider[providerId] ?? "unknown",
      providerId
    );
    await pool.query(
      `INSERT INTO ai_comparison_results (comparison_id, provider_id, model_id, result_id)
       VALUES ($1, $2, $3, $4)`,
      [comparisonId, providerDbId, modelDbId, signalIdByProvider[providerId] ?? null]
    );
  }

  return comparisonId;
}

export function buildScoresRecord(slots: ComparisonSlot[]): Record<string, unknown> {
  const record: Record<string, unknown> = {};
  for (const slot of slots) {
    record[slot.providerId] = slot.outcome === "ok" ? slot.scores : { unavailable: slot.reason };
  }
  return record;
}
OXYGEN_AI_FILE_EOF

echo 'Writing (modified): lib/db/signals.ts'
cat > lib/db/signals.ts << 'OXYGEN_AI_FILE_EOF'
import { getPool } from "./client";
import type { TradeAnalysis } from "../types";

export const PROVIDER_TYPE: Record<string, string> = {
  mock: "mock",
  custom: "custom",
  grok: "xai",
  gemma: "google", // provider identity is Gemma 4; this is only a type label
};

export interface SeededModel {
  providerId: string; // ai_providers.id
  modelDbId: string; // ai_models.id
}

/**
 * Upserts the ai_providers/ai_models rows this app's four provider ids map
 * to, and returns both ids — ai_models.id for trading_signals.generated_by,
 * ai_providers.id for ai_comparison_results.provider_id. Requires the 0002
 * migration (unique constraint on (provider_id, model_id)) — without it
 * this would insert a duplicate model row on every call instead of
 * updating one.
 */
export async function getOrSeedModel(
  providerName: string,
  modelId: string,
  displayName: string
): Promise<SeededModel> {
  const pool = getPool();

  const providerResult = await pool.query<{ id: string }>(
    `INSERT INTO ai_providers (name, type)
     VALUES ($1, $2)
     ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
     RETURNING id`,
    [providerName, PROVIDER_TYPE[providerName] ?? providerName]
  );
  const providerId = providerResult.rows[0].id;

  const modelResult = await pool.query<{ id: string }>(
    `INSERT INTO ai_models (provider_id, model_id, display_name)
     VALUES ($1, $2, $3)
     ON CONFLICT (provider_id, model_id) DO UPDATE SET display_name = EXCLUDED.display_name
     RETURNING id`,
    [providerId, modelId, displayName]
  );
  return { providerId, modelDbId: modelResult.rows[0].id };
}

/**
 * Only SETUP_FOUND results become a trading_signals row — a signal is a
 * specific concrete claim; NO_VALID_SETUP is an absence of one, not a
 * fact worth a row of its own. Returns the new row's id (or null when
 * nothing was inserted) so callers like ai_comparison_results can link
 * to it.
 */
export async function saveSignal(
  instrumentId: string,
  modelDbId: string,
  analysis: TradeAnalysis
): Promise<string | null> {
  if (analysis.status !== "SETUP_FOUND" || !analysis.direction) return null;
  const pool = getPool();
  const result = await pool.query<{ id: string }>(
    `INSERT INTO trading_signals
       (instrument_id, generated_by, direction, entry_low, entry_high,
        stop_loss, target_1, target_2, risk_reward, confidence, status)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'open')
     RETURNING id`,
    [
      instrumentId,
      modelDbId,
      analysis.direction,
      analysis.entry, // entry_low
      analysis.entry, // entry_high — a point estimate, represented as a zero-width range
      analysis.stopLoss,
      analysis.targets[0] ?? null,
      analysis.targets[1] ?? null,
      analysis.riskReward,
      analysis.confidence,
    ]
  );
  return result.rows[0].id;
}
OXYGEN_AI_FILE_EOF

echo 'Writing (modified): lib/providers/index.ts'
cat > lib/providers/index.ts << 'OXYGEN_AI_FILE_EOF'
import type { AIProvider } from "./types";
import { mockProvider } from "./mockProvider";
import { gemmaProvider } from "./gemmaProvider";
import { grokProvider } from "./grokProvider";
import { customAiProvider } from "./customAiProvider";

// Exactly the three providers the blueprint specifies, plus the offline
// mock fallback. Gemini is never a provider in its own right, only
// Gemma's hosted transport (gemmaProvider.ts).
const registry: Record<string, AIProvider> = {
  mock: mockProvider,
  custom: customAiProvider,
  grok: grokProvider,
  gemma: gemmaProvider,
};

export function resolveProvider(requestedId?: string): AIProvider {
  const id = requestedId && registry[requestedId] ? requestedId : "mock";
  const provider = registry[id];
  // Same fallback principle as the blueprint's Provider Router: degrade to
  // a controlled, working path instead of a hard failure when a provider
  // isn't actually configured.
  if (!provider.isConfigured()) return mockProvider;
  return provider;
}

/**
 * For multi-provider comparison, unlike resolveProvider(): does NOT
 * silently substitute mock for an unconfigured provider. A comparison is
 * supposed to show distinct real providers side by side — quietly
 * running mock logic under a "Grok" label would defeat the point.
 * Unconfigured providers are still returned here (reason() will throw;
 * the caller isolates that per-slot — see lib/comparison.ts).
 */
export function getProviderStrict(id: string): AIProvider {
  const provider = registry[id];
  if (!provider) throw new Error(`Unknown provider id: ${id}`);
  return provider;
}

export function listProviders() {
  return Object.values(registry).map((p) => ({
    id: p.id,
    displayName: p.displayName,
    configured: p.isConfigured(),
  }));
}
OXYGEN_AI_FILE_EOF

echo 'Writing (modified): lib/types.ts'
cat > lib/types.ts << 'OXYGEN_AI_FILE_EOF'
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

// Response contract when POST /api/analyze is called with a `providers`
// array (2-3 ids) instead of a single `provider` string. Mirrors the
// blueprint's "response becomes an array of TradeAnalysis objects...
// each still carrying its own source tag" (Passage 1 §10), extended to
// carry the per-slot scoring breakdown and to represent an unconfigured
// provider explicitly rather than only ever returning a full analysis.
const ScoreBreakdownSchema = z.object({
  dataCompleteness: z.number().nullable(),
  indicatorAgreement: z.number().nullable(),
  riskRewardQuality: z.number().nullable(),
  ruleCompliance: z.number().nullable(),
  explanationConsistency: z.number().nullable(),
  historicalValidation: z.literal("not_enough_data"),
  predictionOutcome: z.literal("not_enough_data"),
  confidenceCalibration: z.literal("not_enough_data"),
});
export type ScoreBreakdown = z.infer<typeof ScoreBreakdownSchema>;

export const ComparisonSlotSchema = z.discriminatedUnion("outcome", [
  z.object({
    outcome: z.literal("ok"),
    providerId: z.string(),
    analysis: TradeAnalysisSchema,
    scores: ScoreBreakdownSchema,
  }),
  z.object({
    outcome: z.literal("unavailable"),
    providerId: z.string(),
    reason: z.string(),
  }),
]);
export type ComparisonSlot = z.infer<typeof ComparisonSlotSchema>;

export const ComparisonResponseSchema = z.object({
  mode: z.literal("multi"),
  symbol: z.string(),
  results: z.array(ComparisonSlotSchema),
  persisted: z.boolean(),
  generatedAt: z.string(),
});
export type ComparisonResponse = z.infer<typeof ComparisonResponseSchema>;
OXYGEN_AI_FILE_EOF

echo 'Writing (modified): app/api/analyze/route.ts'
cat > app/api/analyze/route.ts << 'OXYGEN_AI_FILE_EOF'
import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { generateSyntheticOHLCV, computeIndicators } from "@/lib/indicators";
import { resolveProvider } from "@/lib/providers";
import { buildTradeAnalysis } from "@/lib/verify";
import { validateProviderSet, runComparison } from "@/lib/comparison";
import { buildScoresRecord } from "@/lib/db/comparisons";
import type { OHLCVBar } from "@/lib/types";
import { isDbConfigured } from "@/lib/db/client";
import { getOrCreateInstrument, getRecentBars, insertBars } from "@/lib/db/marketData";
import { getOrSeedModel, saveSignal } from "@/lib/db/signals";
import { saveComparison } from "@/lib/db/comparisons";

const RequestSchema = z.object({
  symbol: z.string().min(1).max(20),
  provider: z.string().optional(),
  // 2-3 provider ids triggers multi-provider comparison mode instead of a
  // single analysis. Mirrors the blueprint's mode:"multi" + providers[]
  // contract (Passage 1 §10), as a separate field rather than a
  // redundant "mode" enum, since `provider` (single) vs `providers`
  // (array) is already unambiguous.
  providers: z.array(z.string()).min(2).max(3).optional(),
});

const BARS_NEEDED = 60;

function resolveModelId(providerId: string): string {
  if (providerId === "gemma") return process.env.GEMMA_MODEL_ID || "gemma-4-4b-it";
  if (providerId === "grok") return process.env.GROK_MODEL_ID || "grok-4.6";
  if (providerId === "custom") return process.env.CUSTOM_AI_MODEL_ID || "unset";
  return "mock-v1";
}

/**
 * Shared by both single- and multi-provider modes. Reads persisted bars
 * from the database when configured; generates and stores a fresh
 * (still-synthetic) set the first time a symbol is requested; falls back
 * to ephemeral synthetic data — cleanly, not a crash — if the database
 * isn't configured or a call to it fails.
 */
async function getOrRefreshBars(
  symbol: string
): Promise<{ bars: OHLCVBar[]; instrumentId: string | null; persisted: boolean }> {
  if (!isDbConfigured()) {
    return { bars: generateSyntheticOHLCV(symbol, BARS_NEEDED), instrumentId: null, persisted: false };
  }
  try {
    const instrumentId = await getOrCreateInstrument(symbol);
    const existing = await getRecentBars(instrumentId, BARS_NEEDED);
    if (existing.length >= BARS_NEEDED) {
      return { bars: existing, instrumentId, persisted: true };
    }
    const bars = generateSyntheticOHLCV(symbol, BARS_NEEDED);
    await insertBars(instrumentId, bars);
    return { bars, instrumentId, persisted: true };
  } catch (err) {
    console.error("DB path failed, falling back to ephemeral synthetic data:", err);
    return { bars: generateSyntheticOHLCV(symbol, BARS_NEEDED), instrumentId: null, persisted: false };
  }
}

export async function POST(req: NextRequest) {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  const parsed = RequestSchema.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json(
      { error: "Invalid request", details: parsed.error.flatten() },
      { status: 400 }
    );
  }

  const symbol = parsed.data.symbol.toUpperCase();
  const { bars, instrumentId, persisted } = await getOrRefreshBars(symbol);
  const indicators = computeIndicators(bars);
  const lastBars = bars.slice(-5).map((b) => ({ timestamp: b.timestamp, close: b.close }));

  // ---- Multi-provider comparison mode ----
  if (parsed.data.providers) {
    const validationError = validateProviderSet(parsed.data.providers);
    if (validationError) {
      return NextResponse.json({ error: validationError }, { status: 400 });
    }

    const results = await runComparison(parsed.data.providers, symbol, indicators, lastBars, resolveModelId);

    if (persisted && instrumentId) {
      try {
        const signalIdByProvider: Record<string, string | null> = {};
        const modelIdByProvider: Record<string, string> = {};
        for (const slot of results) {
          modelIdByProvider[slot.providerId] = resolveModelId(slot.providerId);
          if (slot.outcome === "ok" && slot.analysis.status === "SETUP_FOUND") {
            const seeded = await getOrSeedModel(
              slot.providerId,
              resolveModelId(slot.providerId),
              slot.providerId
            );
            signalIdByProvider[slot.providerId] = await saveSignal(instrumentId, seeded.modelDbId, slot.analysis);
          } else {
            signalIdByProvider[slot.providerId] = null;
          }
        }
        await saveComparison(
          parsed.data.providers,
          buildScoresRecord(results),
          signalIdByProvider,
          modelIdByProvider
        );
      } catch (err) {
        // The comparison result itself is still valid and already
        // computed — a failed write shouldn't turn a good response into
        // a 503, same principle as the single-provider path below.
        console.error("Failed to persist comparison (results still returned):", err);
      }
    }

    return NextResponse.json({
      mode: "multi",
      symbol,
      results,
      persisted,
      generatedAt: new Date().toISOString(),
    });
  }

  // ---- Single-provider mode (unchanged contract) ----
  const provider = resolveProvider(parsed.data.provider);
  const source =
    provider.id === "mock"
      ? "mock"
      : provider.id === "custom" && process.env.CUSTOM_AI_SOURCE_TAG === "local_model"
      ? "local_model"
      : "hosted_api";
  const modelId = resolveModelId(provider.id);

  try {
    const reasoning = await provider.reason({ symbol, indicators, lastBars });
    const analysis = buildTradeAnalysis(indicators, reasoning, source, provider.id, modelId, persisted);

    if (persisted && instrumentId && analysis.status === "SETUP_FOUND") {
      try {
        const seeded = await getOrSeedModel(provider.id, modelId, provider.displayName);
        await saveSignal(instrumentId, seeded.modelDbId, analysis);
      } catch (err) {
        console.error("Failed to persist signal (analysis still returned):", err);
      }
    }

    return NextResponse.json(analysis);
  } catch (err) {
    return NextResponse.json(
      {
        error: "Provider call failed",
        detail: err instanceof Error ? err.message : String(err),
      },
      { status: 503 }
    );
  }
}
OXYGEN_AI_FILE_EOF

echo 'Writing (modified): app/page.tsx'
cat > app/page.tsx << 'OXYGEN_AI_FILE_EOF'
"use client";

import { useState, type FormEvent } from "react";
import type { ComparisonResponse, ComparisonSlot, TradeAnalysis } from "@/lib/types";

const COMPARABLE_PROVIDERS = [
  { id: "mock", label: "Mock" },
  { id: "custom", label: "Custom AI" },
  { id: "grok", label: "Grok" },
  { id: "gemma", label: "Gemma 4" },
];

export default function Home() {
  const [symbol, setSymbol] = useState("");
  const [mode, setMode] = useState<"single" | "compare">("single");
  const [providerId, setProviderId] = useState("mock");
  const [compareIds, setCompareIds] = useState<string[]>(["mock", "grok"]);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<TradeAnalysis | null>(null);
  const [comparison, setComparison] = useState<ComparisonResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  function toggleCompareId(id: string) {
    setCompareIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : prev.length >= 3 ? prev : [...prev, id]
    );
  }

  async function runRequest(e: FormEvent) {
    e.preventDefault();
    if (!symbol.trim()) return;
    if (mode === "compare" && (compareIds.length < 2 || compareIds.length > 3)) {
      setError("Pick 2 or 3 providers to compare");
      return;
    }
    setLoading(true);
    setError(null);
    setResult(null);
    setComparison(null);
    try {
      const body =
        mode === "single"
          ? { symbol: symbol.trim(), provider: providerId }
          : { symbol: symbol.trim(), providers: compareIds };
      const res = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) setError(data.error || "Request failed");
      else if (mode === "single") setResult(data);
      else setComparison(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Network error");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen bg-[#0A0C10] px-4 py-10 text-[#E8EAED]">
      <div className={mode === "compare" ? "mx-auto max-w-4xl" : "mx-auto max-w-xl"}>
        <header className="mb-8">
          <p className="font-mono text-xs uppercase tracking-widest text-[#4FD1C5]">
            Oxygen AI — minimal slice
          </p>
          <h1 className="mt-1 text-2xl font-semibold">Deterministic engine, AI reasoning</h1>
          <p className="mt-2 text-sm text-[#7C8591]">
            Indicators are computed here, not by the model — it only reasons over them. Price
            data below is synthetic; see README.
          </p>
        </header>

        <div className="mb-4 flex gap-1 rounded-md border border-[#232830] bg-[#12151B] p-1 text-sm">
          <ModeTab active={mode === "single"} onClick={() => setMode("single")} label="Single" />
          <ModeTab active={mode === "compare"} onClick={() => setMode("compare")} label="Compare" />
        </div>

        <form onSubmit={runRequest} className="flex flex-wrap items-start gap-2">
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            placeholder="e.g. RELIANCE"
            className="flex-1 rounded-md border border-[#232830] bg-[#12151B] px-3 py-2 font-mono text-sm outline-none focus:border-[#4FD1C5] focus:ring-1 focus:ring-[#4FD1C5]"
          />

          {mode === "single" ? (
            <select
              value={providerId}
              onChange={(e) => setProviderId(e.target.value)}
              className="rounded-md border border-[#232830] bg-[#12151B] px-2 py-2 text-sm outline-none focus:border-[#4FD1C5]"
            >
              <option value="mock">Mock (offline)</option>
              <option value="custom">Custom AI</option>
              <option value="gemma">Gemma 4</option>
              <option value="grok">Grok</option>
            </select>
          ) : (
            <div className="flex flex-wrap gap-2">
              {COMPARABLE_PROVIDERS.map((p) => (
                <label
                  key={p.id}
                  className={`cursor-pointer rounded-md border px-2 py-2 font-mono text-xs ${
                    compareIds.includes(p.id)
                      ? "border-[#4FD1C5] text-[#4FD1C5]"
                      : "border-[#232830] text-[#7C8591]"
                  }`}
                >
                  <input
                    type="checkbox"
                    className="mr-1.5"
                    checked={compareIds.includes(p.id)}
                    onChange={() => toggleCompareId(p.id)}
                  />
                  {p.label}
                </label>
              ))}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="rounded-md bg-[#4FD1C5] px-4 py-2 text-sm font-medium text-[#0A0C10] transition hover:opacity-90 disabled:opacity-50"
          >
            {loading ? "Analyzing…" : mode === "single" ? "Analyze" : "Compare"}
          </button>
        </form>

        {error && (
          <div className="mt-6 rounded-md border border-[#F87171]/40 bg-[#F87171]/10 px-4 py-3 text-sm text-[#F87171]">
            {error}
          </div>
        )}

        {result && <ResultCard result={result} />}
        {comparison && <ComparisonGrid data={comparison} />}
      </div>
    </main>
  );
}

function ModeTab({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex-1 rounded px-3 py-1.5 font-mono text-xs uppercase tracking-wide transition ${
        active ? "bg-[#4FD1C5] text-[#0A0C10]" : "text-[#7C8591] hover:text-[#E8EAED]"
      }`}
    >
      {label}
    </button>
  );
}

function ResultCard({ result }: { result: TradeAnalysis }) {
  const noSetup = result.status === "NO_VALID_SETUP";
  const dirColor =
    result.direction === "LONG" ? "#34D399" : result.direction === "SHORT" ? "#F87171" : "#FBBF24";

  return (
    <div className="mt-6 rounded-lg border border-[#232830] bg-[#12151B] p-5">
      <div className="flex items-center justify-between">
        <span
          className="rounded px-2 py-1 font-mono text-xs font-semibold uppercase"
          style={{ color: dirColor, backgroundColor: `${dirColor}1A` }}
        >
          {noSetup ? "No valid setup" : result.direction}
        </span>
        {result.confidence !== null && (
          <span className="font-mono text-sm text-[#7C8591]">
            confidence <span className="text-[#E8EAED]">{result.confidence}</span>
          </span>
        )}
      </div>

      {!noSetup && (
        <dl className="mt-4 grid grid-cols-2 gap-3 font-mono text-sm sm:grid-cols-4">
          <Field label="entry" value={result.entry} />
          <Field label="stop" value={result.stopLoss} />
          <Field label="target 1" value={result.targets[0]} />
          <Field label="target 2" value={result.targets[1]} />
        </dl>
      )}

      <p className="mt-4 text-sm leading-relaxed text-[#C7CCD3]">{result.reasoningSummary}</p>

      {result.supportingEvidence.length > 0 && (
        <ul className="mt-3 space-y-1 font-mono text-xs text-[#7C8591]">
          {result.supportingEvidence.map((ev, i) => (
            <li key={i}>· {ev}</li>
          ))}
        </ul>
      )}

      <div className="mt-5 flex flex-wrap gap-x-4 gap-y-1 border-t border-[#232830] pt-3 font-mono text-[11px] text-[#5B6470]">
        <span>source: {result.source}</span>
        <span>provider: {result.provider}</span>
        <span>model: {result.model}</span>
        <span>rr: {result.riskReward ?? "n/a"}</span>
        <span>persisted: {result.persisted ? "yes" : "no"}</span>
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: number | null | undefined }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-[#5B6470]">{label}</dt>
      <dd className="text-[#E8EAED]">{value ?? "—"}</dd>
    </div>
  );
}

function ComparisonGrid({ data }: { data: ComparisonResponse }) {
  return (
    <div className="mt-6">
      <p className="mb-3 font-mono text-[11px] text-[#5B6470]">
        symbol: {data.symbol} · persisted: {data.persisted ? "yes" : "no"}
      </p>
      <div
        className="grid gap-3"
        style={{ gridTemplateColumns: `repeat(${data.results.length}, minmax(0, 1fr))` }}
      >
        {data.results.map((slot) => (
          <ComparisonColumn key={slot.providerId} slot={slot} />
        ))}
      </div>
    </div>
  );
}

function ComparisonColumn({ slot }: { slot: ComparisonSlot }) {
  if (slot.outcome === "unavailable") {
    return (
      <div className="rounded-lg border border-[#232830] bg-[#12151B] p-4">
        <p className="font-mono text-xs uppercase tracking-widest text-[#5B6470]">{slot.providerId}</p>
        <p className="mt-3 inline-block rounded bg-[#FBBF24]/10 px-2 py-1 font-mono text-[11px] uppercase text-[#FBBF24]">
          unavailable
        </p>
        <p className="mt-2 text-[11px] leading-relaxed text-[#5B6470]">{slot.reason}</p>
      </div>
    );
  }

  const { analysis, scores } = slot;
  const noSetup = analysis.status === "NO_VALID_SETUP";
  const dirColor =
    analysis.direction === "LONG" ? "#34D399" : analysis.direction === "SHORT" ? "#F87171" : "#FBBF24";

  return (
    <div className="rounded-lg border border-[#232830] bg-[#12151B] p-4">
      <p className="font-mono text-xs uppercase tracking-widest text-[#4FD1C5]">{slot.providerId}</p>
      <span
        className="mt-2 inline-block rounded px-2 py-1 font-mono text-[11px] font-semibold uppercase"
        style={{ color: dirColor, backgroundColor: `${dirColor}1A` }}
      >
        {noSetup ? "No setup" : analysis.direction}
      </span>

      {!noSetup && (
        <dl className="mt-3 space-y-1 font-mono text-xs">
          <ScoreRow label="entry" value={analysis.entry} />
          <ScoreRow label="stop" value={analysis.stopLoss} />
          <ScoreRow label="target 1" value={analysis.targets[0]} />
          <ScoreRow label="confidence" value={analysis.confidence} />
          <ScoreRow label="r:r" value={analysis.riskReward} />
        </dl>
      )}

      <p className="mt-3 text-xs leading-relaxed text-[#C7CCD3]">{analysis.reasoningSummary}</p>

      <div className="mt-3 space-y-0.5 border-t border-[#232830] pt-2 font-mono text-[10px] text-[#5B6470]">
        <ScoreRow label="data completeness" value={scores.dataCompleteness} />
        <ScoreRow label="indicator agreement" value={scores.indicatorAgreement} />
        <ScoreRow label="risk/reward quality" value={scores.riskRewardQuality} />
        <ScoreRow label="rule compliance" value={scores.ruleCompliance} />
        <ScoreRow label="explanation consistency" value={scores.explanationConsistency} />
        <p className="flex justify-between">
          <span>historical validation</span>
          <span>not enough data</span>
        </p>
        <p className="flex justify-between">
          <span>prediction outcome</span>
          <span>not enough data</span>
        </p>
        <p className="flex justify-between">
          <span>confidence calibration</span>
          <span>not enough data</span>
        </p>
      </div>
    </div>
  );
}

function ScoreRow({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="flex justify-between">
      <dt className="text-[#5B6470]">{label}</dt>
      <dd className="text-[#E8EAED]">{value ?? "—"}</dd>
    </div>
  );
}
OXYGEN_AI_FILE_EOF

echo 'Writing (modified): package.json'
cat > package.json << 'OXYGEN_AI_FILE_EOF'
{
  "name": "oxygen-ai-slice",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "start": "next start",
    "test": "tsx --test lib/indicators.test.ts lib/providers/index.test.ts lib/scoring.test.ts lib/comparison.test.ts",
    "typecheck": "tsc --noEmit"
  },
  "dependencies": {
    "next": "^14.2.5",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "zod": "^3.23.8",
    "@google/genai": "^1.0.0",
    "pg": "^8.13.0"
  },
  "devDependencies": {
    "typescript": "^5.5.4",
    "tsx": "^4.19.0",
    "@types/node": "^20.14.0",
    "@types/react": "^18.3.3",
    "@types/react-dom": "^18.3.0",
    "@types/pg": "^8.11.10",
    "tailwindcss": "^3.4.7",
    "postcss": "^8.4.40",
    "autoprefixer": "^10.4.19"
  }
}
OXYGEN_AI_FILE_EOF

echo 'Writing (modified): README.md'
cat > README.md << 'OXYGEN_AI_FILE_EOF'
# Oxygen AI — minimal end-to-end slice

A small, real, working slice of the Oxygen AI blueprint. It exists to
prove the one idea that actually matters architecturally: **indicators
are computed deterministically, the AI layer only reasons over them, and
every provider sits behind one swappable interface.** It is not the full
production system — see "Status against the blueprint" below for exactly
what that means.

## Correction applied

The blueprint's own §4.3.1 rule: Gemma 4 is a *provider identity*;
Google's Gemini API is only the *hosted transport* that reaches it —
never a separate provider. `geminiProvider.ts` → `gemmaProvider.ts`, id
`"gemini"` → `"gemma"`, env var → `GOOGLE_AI_API_KEY`. The `@google/genai`
SDK call is unchanged — that's genuinely the only transport to hosted
Gemma 4. A repo-wide grep confirms zero remaining `gemini`/`Gemini`
naming outside of comments explaining this exact distinction.

## What's actually real here

- `lib/indicators.ts` — SMA, RSI(14), ATR(14): real formulas, unit-tested.
- `lib/verify.ts` — the verification stage. Entry/stop/targets computed
  from ATR here, never from the AI's text. A consistency check fails
  closed to `NO_VALID_SETUP` rather than shipping a broken setup.
- `lib/types.ts` — the `TradeAnalysis` schema (Zod): schema-validated
  output, source-tagging (`mock` / `hosted_api` / `local_model`).
- `lib/providers/` — the `AIProvider` interface and four implementations:
  `mockProvider.ts` (offline, no key needed), `customAiProvider.ts`
  (any OpenAI-compatible endpoint — your choice of model, per blueprint
  §4.1), `gemmaProvider.ts` (Gemma 4 via `@google/genai`), and
  `grokProvider.ts` (Grok via a plain `fetch` to xAI).
- `infra/migrations/` — the full database schema (38 tables, plus a
  0002 fix — see below), applied and verified against a real PostgreSQL 16
  + pgvector instance. See `infra/migrations/README.md`.
- `lib/db/` — the route is now actually wired to that schema. When
  `DATABASE_URL` is set: instruments and OHLCV bars are read from and
  written to Postgres (persisted once per symbol, not regenerated every
  request), found signals are saved to `trading_signals`, and providers/
  models are upserted into `ai_providers`/`ai_models` on demand. Falls
  back to the old ephemeral synthetic-data path — cleanly, not a crash —
  if `DATABASE_URL` is unset, or set but unreachable.
- One route (`POST /api/analyze`) and one page running the whole pipeline
  end to end, with correct fallback-to-mock for all three real providers
  and correct fallback-to-ephemeral for the database.

## Status against the blueprint

Being precise about this rather than saying "mostly done" — this is
where the four passages' worth of specification actually stands right
now:

| Blueprint area | Status |
|---|---|
| §4 Custom AI / Grok / Gemma 4 as providers | **Real** — integration layer for all three, interface-uniform, type-checked, correct fallback. None live-tested (needs your keys). Custom AI's "orchestrating agent" / tool-calling half is not built. |
| §4.5 Provider Router — circuit breaker, health checks, model registry | **Not built.** Current fallback is one if-check, not the circuit-breaker-with-cooldown or persisted health-check history the blueprint specifies. |
| §4.6 Multi-provider comparison | **Real.** `POST /api/analyze` with a `providers` array (2-3 ids) runs them concurrently with per-slot failure isolation (`Promise.allSettled` — the JS equivalent of the blueprint's `asyncio.gather(..., return_exceptions=True)`), scores each on 5 of the blueprint's 8 axes for real, and persists to the corrected `ai_comparisons`/`ai_comparison_results` schema. The other 3 axes render as the blueprint's own specified "not enough data yet" placeholder (Passage 4 G10) rather than being faked, since they need `trade_results` outcome history that doesn't exist. UI has a Compare mode with a side-by-side grid. |
| §5 C++/CUDA performance layer | **Not built** — correctly so, per the blueprint's own §5.2/5.3: nothing has been profiled as a bottleneck, and MVP may stay Python by design. |
| §6 Deterministic trading engine | **Partial.** SMA/RSI/ATR only, of MACD/Stochastic/ADX/Bollinger/VWAP. No regime-awareness classifier, no market-structure/S-R detection. |
| §7 RAG & knowledge system | **Not built.** |
| §8 Memory architecture | **Not built** at the app level (schema exists; nothing reads/writes it yet). |
| §9 Database | **Schema real and verified, and wired to the app** for both single analyses and comparisons. `market_instruments`/`market_data`/`trading_signals`/`ai_providers`/`ai_models`/`ai_comparisons`/`ai_comparison_results` are all read/written for real when `DATABASE_URL` is set — verified against a live instance, not asserted (see below). Underlying bar data is still synthetic; `technical_indicators`, `memories`, RAG tables, and the rest of the 38 remain unused. |
| §10 API architecture | **1 of ~7 endpoints** (`/api/analyze`, handling both single and multi-provider modes — roughly `/api/ai/analyze` + `/api/ai/compare` combined). |
| Passage 2 (admin panel, charting, voice, security, error center, backtesting, compliance) | **Not built** — this is nearly all of Passage 2. |
| Passage 3 (infra, CI, broader testing) | **Not built.** |

Doing all of the "not built" rows for real, in one pass, isn't something
a chat session can responsibly claim — it would mean generating
stub/untested code for a GPU-accelerated native layer, a live RAG
pipeline, voice infrastructure, and an admin panel, none of which I could
verify here, and reporting it as done. That's the thing this whole
project is supposed to avoid. What's above is real and checked; what
isn't is named plainly instead of faked.

## Running it

```bash
npm install
cp .env.example .env.local   # optional — add real keys to use a real provider
npm run dev
```

Open http://localhost:3000, type a symbol, hit Analyze.

## Verified before delivery (actually run, not claimed)

```
$ npm install                                            → exit 0
$ npx tsc --noEmit                                        → clean
$ npx tsx --test lib/indicators.test.ts lib/providers/index.test.ts \
           lib/scoring.test.ts lib/comparison.test.ts      → 28/28 pass
$ npx next build                                           → compiled successfully
$ npx next start, then POST /api/analyze for each provider:
    mock   → 200, SETUP_FOUND, real indicator-driven result
    custom → 200, correctly fell back to source: mock (no CUSTOM_AI_* set)
    grok   → 200, correctly fell back to source: mock (no XAI_API_KEY set)
    gemma  → 200, correctly fell back to source: mock (no GOOGLE_AI_API_KEY set)

Multi-provider comparison, verified live:
    POST /api/analyze {symbol, providers:["mock","grok","gemma"]}, no DB
      → 200, mock: outcome ok with a real result; grok + gemma: outcome
        unavailable with their own distinct reasons — isolation confirmed,
        one 200 response, not a partial failure
    Validation: providers:["mock"] → 400 (needs 2-3); ["mock","mock"] →
      400 (no repeats); ["mock","chatgpt"] → 400 (unknown id);
      ["custom","grok","gemma"] → 200 (blueprint's real three-way, accepted)
    Same 3-provider comparison against a live Postgres instance:
      → ai_comparisons: 1 row, provider_set = ["mock","grok","gemma"] as
        jsonb (Passage 1 §9.1's fix, not Revision 2's hardcoded columns)
      → ai_comparison_results: 3 rows, one per provider, each with a real
        model_id — including grok and gemma, which got seeded into
        ai_providers/ai_models even though their slots were unavailable,
        so the foreign key holds either way
      → trading_signals: exactly 1 row, not 3 — only mock's slot actually
        found a setup; grok/gemma's unavailable slots correctly created
        no phantom signals
      → DATABASE_URL pointed at a closed port → 200, persisted: false,
        server log shows the caught ECONNREFUSED — comparison mode
        degrades the same way single-provider mode does

Database wiring (single-provider path), verified against a real
PostgreSQL 16 + pgvector instance:
    2x POST /api/analyze {symbol: HDFCBANK} with DATABASE_URL set
      → both 200, persisted: true, identical entry price both times
      → market_data row count for HDFCBANK: 60 (not 120 — the 2nd call
        read existing bars, it did not regenerate and re-insert)
      → market_instruments: 1 row · ai_providers: 1 row · ai_models: 1 row
        (the 0002 unique-constraint fix confirmed working — no duplicates
        despite two separate seed-model calls)
      → trading_signals: 2 rows (one per SETUP_FOUND call, as intended)
    POST /api/analyze with DATABASE_URL unset
      → 200, persisted: false, unchanged old behavior
  POST /api/analyze {}                                     → 400 (validation works)
$ grep -rln "gemini" **/*.ts **/*.tsx                       → only explanatory comments/test names
$ database: see infra/migrations/README.md for the full log
```

Not verified: a live call to any of the three real providers — blocked by
this sandbox's network policy (locked to package registries), not by
anything in the code. This is exactly why the comparison tests above ran
with grok/gemma unconfigured — the isolation logic is proven, the actual
quality of a real Grok-vs-Gemma disagreement is not, and can't be from
here.

## If you want to keep going

Roughly in priority order: (1) get one real provider live-tested with a
real key — this also unlocks a real (not both-unavailable) comparison,
(2) swap the synthetic bar generator for a real market-data vendor now
that persistence exists to actually store what it returns, (3) fill out
the indicator list, (4) a "select preferred" + rating UI on top of the
comparison view (the DB already has `user_choice_provider_id` waiting for
it), (5) everything else in the status table above.
OXYGEN_AI_FILE_EOF

echo ""
echo "Done. Next steps:"
echo "  npm install          # adds no new deps this time, but harmless to re-run"
echo "  npx tsc --noEmit     # should be clean"
echo "  npx tsx --test lib/indicators.test.ts lib/providers/index.test.ts lib/scoring.test.ts lib/comparison.test.ts"
echo "  npx next build"