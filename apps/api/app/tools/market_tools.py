"""
Market data, historical data, technical indicator, and chart analysis
tools — four of Passage 4 §3.5 (G05)'s 15.

Genuinely read-only, per G05's explicit requirement: none of these ever
calls `insert_bars` or otherwise writes price data. A quote or a
historical range is read from persisted bars when they exist, and
generated ephemerally (never written) when they don't — the same
demo-backed data path `routers/analyze.py` uses, but never mutating.

Passage 1 §4.3's "Live quote/OHLCV lookup via the Market Data
Abstraction layer (P2 §13)" names an abstraction layer that doesn't
exist yet (Phase 5 in the implementation plan). These tools call the
concrete synthetic-data path directly for now; once that abstraction
exists, only the data-fetching call inside each handler changes — the
tool's name, schema, and governance stay identical, which is the whole
point of registering a stable contract now.

The Technical Indicator tool is the one exception to "generate
ephemerally": Passage 4 is explicit that it must be "read-only access to
already-computed indicator values (never computes new ones itself)," so
it reads `technical_indicators` (see `db/indicators_db.py`) and reports
`available: False` — not a fabricated recomputation — when nothing has
been persisted for that symbol yet.
"""

from __future__ import annotations

from ..db.client import is_db_configured
from ..db.indicators_db import get_latest_indicators
from ..db.market_data import get_or_create_instrument, get_recent_bars
from ..engine import market_structure
from ..indicators import generate_synthetic_ohlcv
from .schemas import ToolContext, ToolDefinition, ToolResult

BARS_NEEDED = 60


async def _read_only_bars(symbol: str, count: int):
    """
    Reads persisted bars if enough exist; otherwise generates ephemeral
    ones without writing them. `get_or_create_instrument` registers the
    symbol in the lightweight instrument-reference table if it's new -
    that's bookkeeping about which symbols exist, not a write to market
    data itself, so it doesn't violate the read-only guarantee these
    tools make about price data.

    Falls back to a database entirely when one isn't configured, the
    same way `routers/analyze.py`'s own `get_or_refresh_bars` does -
    these tools must work standalone (e.g. called directly by an admin,
    or in a deployment with no database yet), not only as a side effect
    of a request that happens to have already opened a connection.
    """
    if not is_db_configured():
        return generate_synthetic_ohlcv(symbol, count), None, False
    instrument_id = await get_or_create_instrument(symbol)
    existing = await get_recent_bars(instrument_id, count)
    if len(existing) >= count:
        return existing, instrument_id, True
    return generate_synthetic_ohlcv(symbol, count), instrument_id, False


async def _market_data_handler(input_data: dict, context: ToolContext) -> ToolResult:
    symbol = str(input_data["symbol"]).upper()
    bars, _instrument_id, persisted = await _read_only_bars(symbol, BARS_NEEDED)
    last = bars[-1]
    return ToolResult(
        output={
            "symbol": symbol,
            "lastClose": last.close,
            "lastVolume": last.volume,
            "dataTimestamp": last.timestamp,
            "source": "persisted_demo" if persisted else "ephemeral_demo",
        }
    )


async def _historical_data_handler(input_data: dict, context: ToolContext) -> ToolResult:
    symbol = str(input_data["symbol"]).upper()
    count = min(int(input_data.get("bars", BARS_NEEDED)), 250)
    bars, _instrument_id, persisted = await _read_only_bars(symbol, count)
    return ToolResult(
        output={
            "symbol": symbol,
            "bars": [
                {
                    "timestamp": b.timestamp,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in bars[-count:]
            ],
            "source": "persisted_demo" if persisted else "ephemeral_demo",
        }
    )


async def _technical_indicator_handler(input_data: dict, context: ToolContext) -> ToolResult:
    symbol = str(input_data["symbol"]).upper()
    timeframe = str(input_data.get("timeframe", "1d"))
    if not is_db_configured():
        return ToolResult(
            output={"symbol": symbol, "indicators": {}},
            available=False,
            unavailable_reason="No database is configured — indicators are never persisted without one.",
        )
    instrument_id = await get_or_create_instrument(symbol)
    indicators = await get_latest_indicators(instrument_id, timeframe)
    if not indicators:
        return ToolResult(
            output={"symbol": symbol, "indicators": {}},
            available=False,
            unavailable_reason=(
                f"No indicators have been persisted for {symbol} yet — call "
                "POST /api/ai/analyze for this symbol first to compute and store them."
            ),
        )
    return ToolResult(output={"symbol": symbol, "indicators": indicators})


async def _chart_analysis_handler(input_data: dict, context: ToolContext) -> ToolResult:
    symbol = str(input_data["symbol"]).upper()
    bars, _instrument_id, _persisted = await _read_only_bars(symbol, BARS_NEEDED)
    assessment = market_structure.analyze(bars)
    return ToolResult(
        output={
            "symbol": symbol,
            "structureBias": assessment.structure_bias,
            "nearestSupport": assessment.nearest_support,
            "nearestResistance": assessment.nearest_resistance,
            "breakout": (
                {
                    "direction": assessment.breakout.direction,
                    "level": assessment.breakout.level,
                    "confirmed": assessment.breakout.confirmed,
                }
                if assessment.breakout
                else None
            ),
        }
    )


MARKET_DATA_TOOL = ToolDefinition(
    name="market_data",
    description="Look up the current quote (last close, volume, data timestamp) for an instrument.",
    input_schema={
        "type": "object",
        "required": ["symbol"],
        "properties": {"symbol": {"type": "string", "minLength": 1, "maxLength": 20}},
    },
    output_schema={
        "type": "object",
        "required": ["symbol", "lastClose", "lastVolume", "dataTimestamp", "source"],
        "properties": {
            "symbol": {"type": "string"},
            "lastClose": {"type": "number"},
            "lastVolume": {"type": "number"},
            "dataTimestamp": {"type": "string"},
            "source": {"type": "string"},
        },
    },
    handler=_market_data_handler,
    kind="read_only",
)

HISTORICAL_DATA_TOOL = ToolDefinition(
    name="historical_data",
    description="Look up historical OHLCV bars for an instrument, most recent last.",
    input_schema={
        "type": "object",
        "required": ["symbol"],
        "properties": {
            "symbol": {"type": "string", "minLength": 1, "maxLength": 20},
            "bars": {"type": "integer", "minimum": 1, "maximum": 250},
        },
    },
    output_schema={
        "type": "object",
        "required": ["symbol", "bars", "source"],
        "properties": {
            "symbol": {"type": "string"},
            "bars": {"type": "array"},
            "source": {"type": "string"},
        },
    },
    handler=_historical_data_handler,
    kind="read_only",
    timeout_seconds=15.0,
)

TECHNICAL_INDICATOR_TOOL = ToolDefinition(
    name="technical_indicator",
    description=(
        "Read-only access to already-computed indicator values for an instrument. "
        "Never computes a new indicator value itself."
    ),
    input_schema={
        "type": "object",
        "required": ["symbol"],
        "properties": {
            "symbol": {"type": "string", "minLength": 1, "maxLength": 20},
            "timeframe": {"type": "string", "enum": ["1d"]},
        },
    },
    output_schema={
        "type": "object",
        "required": ["symbol", "indicators"],
        "properties": {"symbol": {"type": "string"}, "indicators": {"type": "object"}},
    },
    handler=_technical_indicator_handler,
    kind="read_only",
)

CHART_ANALYSIS_TOOL = ToolDefinition(
    name="chart_analysis",
    description="Structured description of chart state — structure bias, nearest support/resistance, breakout status.",
    input_schema={
        "type": "object",
        "required": ["symbol"],
        "properties": {"symbol": {"type": "string", "minLength": 1, "maxLength": 20}},
    },
    output_schema={
        "type": "object",
        "required": ["symbol", "structureBias"],
        "properties": {
            "symbol": {"type": "string"},
            "structureBias": {"type": "string"},
            "nearestSupport": {"type": ["number", "null"]},
            "nearestResistance": {"type": ["number", "null"]},
            "breakout": {"type": ["object", "null"]},
        },
    },
    handler=_chart_analysis_handler,
    kind="read_only",
)