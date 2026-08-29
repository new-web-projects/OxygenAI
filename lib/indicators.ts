import type { OHLCVBar, IndicatorBundle } from "./types";

export function sma(closes: number[], period: number): number | null {
  if (closes.length < period) return null;
  const window = closes.slice(-period);
  return window.reduce((a, b) => a + b, 0) / period;
}

export function rsi(closes: number[], period = 14): number | null {
  if (closes.length < period + 1) return null;
  let gains = 0;
  let losses = 0;
  for (let i = closes.length - period; i < closes.length; i++) {
    const change = closes[i] - closes[i - 1];
    if (change > 0) gains += change;
    else losses += Math.abs(change);
  }
  const avgGain = gains / period;
  const avgLoss = losses / period;
  if (avgLoss === 0) return 100;
  const rs = avgGain / avgLoss;
  return 100 - 100 / (1 + rs);
}

export function atr(bars: OHLCVBar[], period = 14): number | null {
  if (bars.length < period + 1) return null;
  const trueRanges: number[] = [];
  for (let i = bars.length - period; i < bars.length; i++) {
    const cur = bars[i];
    const prevClose = bars[i - 1].close;
    const tr = Math.max(
      cur.high - cur.low,
      Math.abs(cur.high - prevClose),
      Math.abs(cur.low - prevClose)
    );
    trueRanges.push(tr);
  }
  return trueRanges.reduce((a, b) => a + b, 0) / period;
}

export function computeIndicators(bars: OHLCVBar[]): IndicatorBundle {
  const closes = bars.map((b) => b.close);
  const sma20 = sma(closes, 20);
  const sma50 = sma(closes, 50);
  const rsi14 = rsi(closes, 14);
  const atr14 = atr(bars, 14);
  const lastClose = closes[closes.length - 1];

  let trend: "up" | "down" | "flat" = "flat";
  if (sma20 !== null && sma50 !== null) {
    if (sma20 > sma50 * 1.001) trend = "up";
    else if (sma20 < sma50 * 0.999) trend = "down";
  }

  return { sma20, sma50, rsi14, atr14, lastClose, trend };
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

/**
 * FOR DEMO ONLY. Deterministic pseudo-random walk seeded from the symbol
 * string — not real market data. There is no NSE/BSE vendor wired up here
 * (see README). Deterministic so tests are reproducible.
 */
export function generateSyntheticOHLCV(symbol: string, bars = 60): OHLCVBar[] {
  let seed = 0;
  for (const ch of symbol) seed = (seed * 31 + ch.charCodeAt(0)) % 100000;
  if (seed === 0) seed = 42;

  const rand = () => {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    return seed / 2147483648;
  };

  const result: OHLCVBar[] = [];
  let price = 100 + (seed % 400);
  const now = Date.now();
  for (let i = bars; i > 0; i--) {
    const change = (rand() - 0.48) * price * 0.02;
    const open = price;
    const close = Math.max(1, price + change);
    const high = Math.max(open, close) + rand() * price * 0.005;
    const low = Math.min(open, close) - rand() * price * 0.005;
    const volume = Math.floor(10000 + rand() * 90000);
    result.push({
      timestamp: new Date(now - i * 24 * 60 * 60 * 1000).toISOString(),
      open: round2(open),
      high: round2(high),
      low: round2(low),
      close: round2(close),
      volume,
    });
    price = close;
  }
  return result;
}
