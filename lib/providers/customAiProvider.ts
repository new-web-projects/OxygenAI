import type { AIProvider, AnalysisContext, ProviderReasoning } from "./types";

// "Custom AI" per the blueprint is deliberately not a fixed vendor — §4.1
// defines it as an orchestrating agent over "any strong tool-calling LLM
// behind the AIProvider interface," the owner's choice. This is that
// choice made concrete: any OpenAI-compatible chat-completions endpoint,
// configured via env vars. Point CUSTOM_AI_BASE_URL at OpenAI, a
// self-hosted Ollama/vLLM server, or a router like OpenRouter, and this
// becomes whichever model you pick — no code change needed.
//
// NOT implemented here: the "orchestrating agent" half of the spec — tool
// calling against the 15-tool registry, the RAG -> feedback loop ->
// fine-tuning evolution path (blueprint §4.1, §4.8). This is the provider
// integration layer only. See README.
const BASE_URL = process.env.CUSTOM_AI_BASE_URL || "";
const MODEL_ID = process.env.CUSTOM_AI_MODEL_ID || "";

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

export const customAiProvider: AIProvider = {
  id: "custom",
  displayName: "Custom AI (your configured model)",
  isConfigured: () => Boolean(process.env.CUSTOM_AI_API_KEY && BASE_URL && MODEL_ID),
  async reason(context: AnalysisContext): Promise<ProviderReasoning> {
    if (!process.env.CUSTOM_AI_API_KEY || !BASE_URL || !MODEL_ID) {
      throw new Error(
        "CUSTOM_AI_API_KEY, CUSTOM_AI_BASE_URL, and CUSTOM_AI_MODEL_ID must all be set"
      );
    }

    const res = await fetch(`${BASE_URL.replace(/\/$/, "")}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${process.env.CUSTOM_AI_API_KEY}`,
      },
      body: JSON.stringify({
        model: MODEL_ID,
        messages: [{ role: "user", content: buildPrompt(context) }],
      }),
    });

    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`Custom AI endpoint error ${res.status}: ${detail.slice(0, 200)}`);
    }

    const data = await res.json();
    const text: string = data?.choices?.[0]?.message?.content ?? "";
    const jsonMatch = text.match(/\{[\s\S]*\}/);
    if (!jsonMatch) {
      throw new Error("Custom AI response did not contain parseable JSON");
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