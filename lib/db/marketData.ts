import { getPool } from "./client";
import type { OHLCVBar } from "../types";

export async function getOrCreateInstrument(
  symbol: string,
  exchange = "NSE",
  instrumentType = "EQUITY"
): Promise<string> {
  const pool = getPool();
  const result = await pool.query<{ id: string }>(
    `INSERT INTO market_instruments (symbol, exchange, instrument_type)
     VALUES ($1, $2, $3)
     ON CONFLICT (symbol, exchange) DO UPDATE SET symbol = EXCLUDED.symbol
     RETURNING id`,
    [symbol, exchange, instrumentType]
  );
  return result.rows[0].id;
}

export async function getRecentBars(
  instrumentId: string,
  limit: number,
  timeframe = "1d"
): Promise<OHLCVBar[]> {
  const pool = getPool();
  const result = await pool.query<{
    ts: Date;
    open: string;
    high: string;
    low: string;
    close: string;
    volume: string;
  }>(
    `SELECT ts, open, high, low, close, volume FROM market_data
     WHERE instrument_id = $1 AND timeframe = $2
     ORDER BY ts DESC LIMIT $3`,
    [instrumentId, timeframe, limit]
  );
  // DESC for the LIMIT to grab the most recent N, then reversed back to
  // chronological order — indicators.ts expects oldest-to-newest.
  return result.rows
    .map((r) => ({
      timestamp: r.ts.toISOString(),
      open: Number(r.open),
      high: Number(r.high),
      low: Number(r.low),
      close: Number(r.close),
      volume: Number(r.volume),
    }))
    .reverse();
}

export async function insertBars(
  instrumentId: string,
  bars: OHLCVBar[],
  timeframe = "1d",
  source = "synthetic-demo"
): Promise<void> {
  if (bars.length === 0) return;
  const pool = getPool();

  const values: string[] = [];
  const params: unknown[] = [];
  bars.forEach((bar, i) => {
    const base = i * 9;
    values.push(
      `($${base + 1},$${base + 2},$${base + 3},$${base + 4},$${base + 5},$${base + 6},$${base + 7},$${base + 8},$${base + 9})`
    );
    params.push(
      instrumentId,
      bar.timestamp,
      bar.open,
      bar.high,
      bar.low,
      bar.close,
      bar.volume,
      timeframe,
      source
    );
  });

  // data_mode is left to its schema default ('demo') — accurate, since
  // this is still synthetic data, not a real vendor feed.
  await pool.query(
    `INSERT INTO market_data (instrument_id, ts, open, high, low, close, volume, timeframe, source)
     VALUES ${values.join(",")}`,
    params
  );
}