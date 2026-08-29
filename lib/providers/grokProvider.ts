import type { AIProvider, AnalysisContext, ProviderReasoning } from "./types";

// xAI's API is OpenAI-compatible (chat completions shape), so this uses a
// plain fetch rather than pulling in an SDK for one endpoint — same
// "don't add a dependency you don't need" principle the blueprint itself
// states for plugin/library choices.
const XAI_BASE_URL = "https://api.x.ai/v1";
const MODEL_ID = process.env.GROK_MODEL_ID || "grok-4.6";

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

export const grokProvider: AIProvider = {
  id: "grok",
  displayName: "Grok (xAI)",
  isConfigured: () => Boolean(process.env.XAI_API_KEY),
  async reason(context: AnalysisContext): Promise<ProviderReasoning> {
    if (!process.env.XAI_API_KEY) {
      throw new Error("XAI_API_KEY is not set");
    }

    const res = await fetch(`${XAI_BASE_URL}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${process.env.XAI_API_KEY}`,
      },
      body: JSON.stringify({
        model: MODEL_ID,
        messages: [{ role: "user", content: buildPrompt(context) }],
      }),
    });

    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`xAI API error ${res.status}: ${detail.slice(0, 200)}`);
    }

    const data = await res.json();
    const text: string = data?.choices?.[0]?.message?.content ?? "";
    const jsonMatch = text.match(/\{[\s\S]*\}/);
    if (!jsonMatch) {
      throw new Error("Grok response did not contain parseable JSON");
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