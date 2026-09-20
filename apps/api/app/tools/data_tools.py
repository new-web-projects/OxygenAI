"""
Database search tool — Passage 4 §3.5 (G05): "Read-only structured
query over trading_signals / trade_results."

A plain SELECT, never a raw query string built from the caller's input
— `symbol` and `provider` are bound parameters, and the only shape of
query this tool can ever run is the one written here. This is what "no
raw code-execution or shell tool" (G05) means applied to a database
tool specifically: the tool can look, never execute.

`trade_results` has no rows yet (no backtesting/paper-trading system
exists — that's Phase-later work), so the LEFT JOIN to it always comes
back NULL today. It's written as a real join rather than omitted,
so the moment that subsystem starts writing outcomes, this tool starts
reporting them automatically, with no code change here.
"""

from __future__ import annotations

from ..db.client import get_pool, is_db_configured
from .schemas import ToolContext, ToolDefinition, ToolResult


async def _database_search_handler(input_data: dict, context: ToolContext) -> ToolResult:
    if not is_db_configured():
        return ToolResult(
            output={"count": 0, "signals": []},
            available=False,
            unavailable_reason="No database is configured — there is nothing to search.",
        )
    symbol = input_data.get("symbol")
    provider_name = input_data.get("provider")
    limit = min(int(input_data.get("limit", 10)), 50)

    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT
            ts.id, mi.symbol, ts.direction, ts.entry_low, ts.stop_loss,
            ts.target_1, ts.risk_reward, ts.confidence, ts.status, ts.created_at,
            p.name AS provider_name,
            tr.outcome, tr.pnl
        FROM trading_signals ts
        JOIN market_instruments mi ON mi.id = ts.instrument_id
        LEFT JOIN ai_models am ON am.id = ts.generated_by
        LEFT JOIN ai_providers p ON p.id = am.provider_id
        LEFT JOIN trade_setups tsu ON tsu.signal_id = ts.id
        LEFT JOIN trade_results tr ON tr.trade_setup_id = tsu.id
        WHERE ($1::text IS NULL OR mi.symbol = $1)
          AND ($2::text IS NULL OR p.name = $2)
        ORDER BY ts.created_at DESC
        LIMIT $3
        """,
        symbol.upper() if symbol else None,
        provider_name,
        limit,
    )

    return ToolResult(
        output={
            "count": len(rows),
            "signals": [
                {
                    "id": str(r["id"]),
                    "symbol": r["symbol"],
                    "provider": r["provider_name"],
                    "direction": r["direction"],
                    "entry": float(r["entry_low"]) if r["entry_low"] is not None else None,
                    "stopLoss": float(r["stop_loss"]),
                    "target1": float(r["target_1"]) if r["target_1"] is not None else None,
                    "riskReward": float(r["risk_reward"]) if r["risk_reward"] is not None else None,
                    "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                    "status": r["status"],
                    "outcome": r["outcome"],
                    "pnl": float(r["pnl"]) if r["pnl"] is not None else None,
                    "createdAt": r["created_at"].isoformat(),
                }
                for r in rows
            ],
        }
    )


DATABASE_SEARCH_TOOL = ToolDefinition(
    name="database_search",
    description="Read-only structured query over past trading signals and, where recorded, their outcomes.",
    input_schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "maxLength": 20},
            "provider": {"type": "string", "maxLength": 50},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
    },
    output_schema={
        "type": "object",
        "required": ["count", "signals"],
        "properties": {"count": {"type": "integer"}, "signals": {"type": "array"}},
    },
    handler=_database_search_handler,
    kind="read_only",
)