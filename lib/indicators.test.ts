import { test } from "node:test";
import assert from "node:assert/strict";
import {
  sma,
  rsi,
  atr,
  computeIndicators,
  generateSyntheticOHLCV,
} from "./indicators";

test("sma returns null when there isn't enough data", () => {
  assert.equal(sma([1, 2, 3], 5), null);
});

test("sma computes a plain average", () => {
  assert.equal(sma([1, 2, 3, 4, 5], 5), 3);
});

test("rsi is 100 when there are no losses in the window", () => {
  const closes = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24];
  assert.equal(rsi(closes, 14), 100);
});

test("rsi stays within 0-100 for mixed up/down data", () => {
  const closes = [10, 11, 9, 12, 8, 13, 7, 14, 6, 15, 5, 16, 4, 17, 3];
  const value = rsi(closes, 14);
  assert.ok(value !== null && value >= 0 && value <= 100);
});

test("atr is non-negative on synthetic data", () => {
  const bars = generateSyntheticOHLCV("TEST", 30);
  const value = atr(bars, 14);
  assert.ok(value !== null && value >= 0);
});

test("generateSyntheticOHLCV is deterministic for the same symbol", () => {
  const a = generateSyntheticOHLCV("RELIANCE", 10);
  const b = generateSyntheticOHLCV("RELIANCE", 10);
  assert.deepEqual(a, b);
});

test("generateSyntheticOHLCV differs across symbols", () => {
  const a = generateSyntheticOHLCV("RELIANCE", 10);
  const b = generateSyntheticOHLCV("TCS", 10);
  assert.notDeepEqual(a, b);
});

test("computeIndicators produces a full bundle once there's enough history", () => {
  const bars = generateSyntheticOHLCV("TCS", 60);
  const bundle = computeIndicators(bars);
  assert.ok(bundle.sma20 !== null);
  assert.ok(bundle.sma50 !== null);
  assert.ok(bundle.rsi14 !== null);
  assert.ok(bundle.atr14 !== null);
  assert.ok(["up", "down", "flat"].includes(bundle.trend));
});

test("computeIndicators degrades gracefully on too little history", () => {
  const bars = generateSyntheticOHLCV("NEWLISTING", 5);
  const bundle = computeIndicators(bars);
  assert.equal(bundle.sma20, null);
  assert.equal(bundle.sma50, null);
  assert.equal(bundle.trend, "flat");
});
