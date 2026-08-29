import { GoogleGenAI } from "@google/genai";
import type { AIProvider, AnalysisContext, ProviderReasoning } from "./types";

// Identity vs. transport, per the blueprint's own §4.3.1 rule: Gemma 4 is
// the provider identity; Google's Gemini API is only the hosted transport
// that reaches it. This file is GemmaProvider — nothing here should be
// named or exposed as a separate "Gemini" provider.
//
// Verify MODEL_ID against Google's current model list before relying on
// it — Gemma 4 model ID strings have shown minor naming variation across
// release waves (see README). Override with GEMMA_MODEL_ID if it's changed.
const MODEL_ID = process.env.GEMMA_MODEL_ID || "gemma-4-4b-it";

function buildPrompt(context: AnalysisContext): string {
  const { symbol, indicators } = context;
  return `You are a trading analysis reasoner. You never invent numbers — you only reason over the indicators given below, which were computed deterministically outside of you.

Symbol: ${symbol}
Last close: ${indicators.lastClose}
SMA(20): ${indicators.sma20 ?? "n/a"}
SMA(50): ${indicators.sma50 ?? "n/a"}
RSI(14): ${indicators.rsi14 ?? "n/a"}
ATR(14): ${indicators.atr14 ?? "n/a"}
Trend: ${indicators.trend}

Respond ONLY with JSON matching exactly this shape, nothing else, no markdown fences:
{"direction": "LONG" or "SHORT" or null, "confidence": number 0-100 or null, "reasoningSummary": string, "supportingEvidence": string[]}

If there is no valid setup, set direction and confidence to null and explain why in reasoningSummary.`;
}

export const gemmaProvider: AIProvider = {
  id: "gemma",
  displayName: "Gemma 4 (hosted via Gemini API transport)",
  isConfigured: () => Boolean(process.env.GOOGLE_AI_API_KEY),
  async reason(context: AnalysisContext): Promise<ProviderReasoning> {
    if (!process.env.GOOGLE_AI_API_KEY) {
      throw new Error("GOOGLE_AI_API_KEY is not set");
    }

    // @google/genai is the SDK Google ships for reaching Gemini AND Gemma —
    // using it is the transport, not a second provider. See comment above.
    const ai = new GoogleGenAI({ apiKey: process.env.GOOGLE_AI_API_KEY });
    const response = await ai.models.generateContent({
      model: MODEL_ID,
      contents: buildPrompt(context),
    });

    const text = response.text ?? "";
    const jsonMatch = text.match(/\{[\s\S]*\}/);
    if (!jsonMatch) {
      throw new Error("Gemma response did not contain parseable JSON");
    }

    const parsed = JSON.parse(jsonMatch[0]);
    return {
      direction: parsed.direction ?? null,
      confidence: parsed.confidence ?? null,
      reasoningSummary: parsed.reasoningSummary ?? "",
      supportingEvidence: Array.isArray(parsed.supportingEvidence)
        ? parsed.supportingEvidence
        : [],
    };
  },
};