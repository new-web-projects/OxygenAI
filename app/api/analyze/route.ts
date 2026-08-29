import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { generateSyntheticOHLCV, computeIndicators } from "@/lib/indicators";
import { resolveProvider } from "@/lib/providers";
import { buildTradeAnalysis } from "@/lib/verify";
import type { OHLCVBar } from "@/lib/types";
import { isDbConfigured } from "@/lib/db/client";
import { getOrCreateInstrument, getRecentBars, insertBars } from "@/lib/db/marketData";
import { getOrSeedModel, saveSignal } from "@/lib/db/signals";

const RequestSchema = z.object({
  symbol: z.string().min(1).max(20),
  provider: z.string().optional(),
});

const BARS_NEEDED = 60;

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
  const provider = resolveProvider(parsed.data.provider);

  const source =
    provider.id === "mock"
      ? "mock"
      : provider.id === "custom" && process.env.CUSTOM_AI_SOURCE_TAG === "local_model"
      ? "local_model"
      : "hosted_api";

  const modelId =
    provider.id === "gemma"
      ? process.env.GEMMA_MODEL_ID || "gemma-4-4b-it"
      : provider.id === "grok"
      ? process.env.GROK_MODEL_ID || "grok-4.6"
      : provider.id === "custom"
      ? process.env.CUSTOM_AI_MODEL_ID || "unset"
      : "mock-v1";

  // Try the database path; fall back to ephemeral synthetic data if it
  // isn't configured, or if it's configured but the call fails — same
  // controlled-degradation principle as the provider fallback, applied to
  // persistence instead of reasoning.
  let bars: OHLCVBar[];
  let instrumentId: string | null = null;
  let persisted = false;

  if (isDbConfigured()) {
    try {
      instrumentId = await getOrCreateInstrument(symbol);
      const existing = await getRecentBars(instrumentId, BARS_NEEDED);
      if (existing.length < BARS_NEEDED) {
        // NOTE: still synthetic — there is no real market-data vendor
        // wired up. What's real here is that it's now generated once and
        // persisted, not regenerated fresh (and different) on every call.
        bars = generateSyntheticOHLCV(symbol, BARS_NEEDED);
        await insertBars(instrumentId, bars);
      } else {
        bars = existing;
      }
      persisted = true;
    } catch (err) {
      console.error("DB path failed, falling back to ephemeral synthetic data:", err);
      bars = generateSyntheticOHLCV(symbol, BARS_NEEDED);
      persisted = false;
    }
  } else {
    bars = generateSyntheticOHLCV(symbol, BARS_NEEDED);
  }

  const indicators = computeIndicators(bars);

  try {
    const reasoning = await provider.reason({
      symbol,
      indicators,
      lastBars: bars.slice(-5).map((b) => ({ timestamp: b.timestamp, close: b.close })),
    });

    const analysis = buildTradeAnalysis(
      indicators,
      reasoning,
      source,
      provider.id,
      modelId,
      persisted
    );

    if (persisted && instrumentId && analysis.status === "SETUP_FOUND") {
      try {
        const modelDbId = await getOrSeedModel(provider.id, modelId, provider.displayName);
        await saveSignal(instrumentId, modelDbId, analysis);
      } catch (err) {
        // The analysis itself is still valid and already computed —
        // a failed write shouldn't turn a good response into a 503.
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
