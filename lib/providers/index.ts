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

export function listProviders() {
  return Object.values(registry).map((p) => ({
    id: p.id,
    displayName: p.displayName,
    configured: p.isConfigured(),
  }));
}