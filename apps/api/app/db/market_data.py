"""
Port of lib/db/marketData.ts — instrument upsert, reading persisted bars,
appending new ones.
"""

from __future__ import annotations

from datetime import datetime

from .client import get_pool
from ..schemas import OHLCVBar


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


async def get_or_create_instrument(symbol: str, exchange: str = "NSE", instrument_type: str = "EQUITY") -> str:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO market_instruments (symbol, exchange, instrument_type)
        VALUES ($1, $2, $3)
        ON CONFLICT (symbol, exchange) DO UPDATE SET symbol = EXCLUDED.symbol
        RETURNING id
        """,
        symbol,
        exchange,
        instrument_type,
    )
    return str(row["id"])


async def get_recent_bars(instrument_id: str, limit: int, timeframe: str = "1d") -> list[OHLCVBar]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT ts, open, high, low, close, volume FROM market_data
        WHERE instrument_id = $1 AND timeframe = $2
        ORDER BY ts DESC LIMIT $3
        """,
        instrument_id,
        timeframe,
        limit,
    )
    # DESC for the LIMIT to grab the most recent N, then reversed back to
    # chronological order — the indicator engine expects oldest-to-newest.
    bars = [
        OHLCVBar(
            timestamp=r["ts"].isoformat().replace("+00:00", "Z"),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"]),
        )
        for r in rows
    ]
    return list(reversed(bars))


async def insert_bars(
    instrument_id: str, bars: list[OHLCVBar], timeframe: str = "1d", source: str = "synthetic-demo"
) -> None:
    if not bars:
        return
    pool = await get_pool()
    # data_mode is left to its schema default ('demo') — accurate, since
    # this is still synthetic data, not a real vendor feed.
    await pool.executemany(
        """
        INSERT INTO market_data (instrument_id, ts, open, high, low, close, volume, timeframe, source)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        [
            (
                instrument_id,
                _parse_ts(bar.timestamp),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                timeframe,
                source,
            )
            for bar in bars
        ],
    )
