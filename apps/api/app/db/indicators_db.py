"""
`technical_indicators` — confirmed unused by any code path in the
original audit. First used here: the deterministic engine's output is
persisted, one row per named indicator (matching the table's own
normalized shape — `indicator_name`, `value jsonb` — rather than one row
per bundle), and the Technical Indicator tool (`tools/market_tools.py`)
reads it back.

This is what makes that tool's contract literal rather than aspirational
— Passage 4 §3.5 (G05) specifies it as "Read-only access to
already-computed indicator values (never computes new ones itself)",
which requires something to have written those values first.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..schemas import IndicatorBundle
from .client import get_pool


def _parse_ts(ts: str) -> datetime:
    """Same conversion `db/market_data.py` uses — asyncpg needs a real
    datetime for a `timestamp with time zone` column, not an ISO string."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# Indicators with more than one sub-value are stored as a small jsonb
# object; scalars are stored as a bare number. Either way `value` is
# always valid JSON, matching the column's jsonb type.
_SCALAR_FIELDS = ("sma20", "sma50", "ema20", "ema50", "rsi14", "atr14", "vwap")


async def persist_indicator_bundle(
    instrument_id: str, ts: str, timeframe: str, bundle: IndicatorBundle
) -> None:
    pool = await get_pool()
    rows: list[tuple[str, Any]] = []

    for field_name in _SCALAR_FIELDS:
        value = getattr(bundle, field_name)
        if value is not None:
            rows.append((field_name, value))

    if bundle.macd is not None:
        rows.append(
            ("macd", {"macd": bundle.macd, "signal": bundle.macdSignal, "histogram": bundle.macdHistogram})
        )
    if bundle.stochasticK is not None:
        rows.append(("stochastic", {"k": bundle.stochasticK, "d": bundle.stochasticD}))
    if bundle.adx is not None:
        rows.append(("adx", {"adx": bundle.adx, "plusDi": bundle.plusDi, "minusDi": bundle.minusDi}))
    if bundle.bollingerUpper is not None:
        rows.append(
            (
                "bollinger",
                {
                    "upper": bundle.bollingerUpper,
                    "middle": bundle.bollingerMiddle,
                    "lower": bundle.bollingerLower,
                    "percentB": bundle.bollingerPercentB,
                },
            )
        )

    if not rows:
        return

    await pool.executemany(
        """
        INSERT INTO technical_indicators (instrument_id, ts, timeframe, indicator_name, value)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        """,
        [(instrument_id, _parse_ts(ts), timeframe, name, json.dumps(value)) for name, value in rows],
    )


async def get_latest_indicators(instrument_id: str, timeframe: str) -> dict[str, Any]:
    """
    Returns {indicator_name: value} using each name's most recent `ts`
    for this instrument/timeframe. Empty dict, never an exception, when
    nothing has been persisted yet — the Technical Indicator tool reports
    that as `available: False`, not as a schema-validation failure.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (indicator_name) indicator_name, value, ts
        FROM technical_indicators
        WHERE instrument_id = $1 AND timeframe = $2
        ORDER BY indicator_name, ts DESC
        """,
        instrument_id,
        timeframe,
    )
    return {row["indicator_name"]: json.loads(row["value"]) for row in rows}