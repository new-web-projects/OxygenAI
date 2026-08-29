import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { generateSyntheticOHLCV, computeIndicators } from "@/lib/indicators";
import { resolveProvider } from "@/lib/providers";
import { buildTradeAnalysis } from "@/lib/verify";

const RequestSchema = z.object({
  symbol: z.string().min(1).max(20),
  provider: z.string().optional(),
});

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

  // NOTE: synthetic data — there is no real market-data vendor wired up
  // yet. See README for what a real integration needs.
  const bars = generateSyntheticOHLCV(symbol);
  const indicators = computeIndicators(bars);

  try {
    const reasoning = await provider.reason({
      symbol,
      indicators,
      lastBars: bars.slice(-5).map((b) => ({ timestamp: b.timestamp, close: b.close })),
    });

    const analysis = buildTradeAnalysis(indicators, reasoning, source, provider.id, modelId);
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
