import { test } from "node:test";
import assert from "node:assert/strict";
import { resolveProvider, listProviders } from "./index";

test("resolveProvider falls back to mock for an unknown id", () => {
  const provider = resolveProvider("does-not-exist");
  assert.equal(provider.id, "mock");
});

test("resolveProvider falls back to mock for gemma when no key is configured", () => {
  assert.equal(resolveProvider("gemma").id, "mock"); // no GOOGLE_AI_API_KEY set here
});

test("resolveProvider falls back to mock for grok when no key is configured", () => {
  assert.equal(resolveProvider("grok").id, "mock"); // no XAI_API_KEY set here
});

test("resolveProvider falls back to mock for custom when unconfigured", () => {
  assert.equal(resolveProvider("custom").id, "mock"); // no CUSTOM_AI_* set here
});

test("the registry is exactly mock, custom, grok, gemma — no gemini entry", () => {
  const ids = listProviders()
    .map((p) => p.id)
    .sort();
  assert.deepEqual(ids, ["custom", "gemma", "grok", "mock"]);
});