import type { IndicatorBundle } from "../types";

export interface AnalysisContext {
  symbol: string;
  indicators: IndicatorBundle;
  lastBars: { timestamp: string; close: number }[];
}

export interface ProviderReasoning {
  direction: "LONG" | "SHORT" | null;
  confidence: number | null;
  reasoningSummary: string;
  supportingEvidence: string[];
}

// Every provider (mock, Gemma today; Grok, Custom AI later) implements this
// one interface. Adding a provider is a new file, not a change to the
// engine, the schema, or the API route — the actual point of the
// blueprint's "provider-agnostic AI layer".
export interface AIProvider {
  id: string;
  displayName: string;
  isConfigured(): boolean;
  reason(context: AnalysisContext): Promise<ProviderReasoning>;
}
