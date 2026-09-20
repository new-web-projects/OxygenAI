"""
Risk calculator and position sizing calculator — Passage 4 §3.5 (G05):
"Pure-function wrapper[s] around the risk engine / position sizing
(P1 §6)."

Literally pure: no database call, no network call, no dependency on
anything except the numbers the caller passes in and the same
`engine.risk_engine` module `verify.py` itself uses. This is also the
fastest and most reliably available pair of tools in the registry —
nothing here can time out or hit a rate-limited external vendor, since
there is no external anything to reach.
"""

from __future__ import annotations

from ..config import RiskSettings, get_settings
from ..engine import risk_engine
from .schemas import ToolContext, ToolDefinition, ToolResult


async def _risk_calculator_handler(input_data: dict, context: ToolContext) -> ToolResult:
    direction = input_data["direction"]
    entry = float(input_data["entry"])
    atr = float(input_data["atr"])
    settings = get_settings().risk
    min_rr = float(input_data.get("minRiskReward", settings.min_risk_reward))
    effective = RiskSettings(
        account_equity=settings.account_equity,
        max_risk_per_trade_pct=settings.max_risk_per_trade_pct,
        sizing_method=settings.sizing_method,
        atr_stop_multiple=float(input_data.get("atrStopMultiple", settings.atr_stop_multiple)),
        min_risk_reward=min_rr,
        target_r_multiples=settings.target_r_multiples,
    )
    assessment = risk_engine.assess(direction=direction, entry=entry, atr=atr, settings=effective)
    return ToolResult(
        output={
            "direction": direction,
            "entry": entry,
            "stopLoss": assessment.stop_loss,
            "targets": assessment.targets,
            "riskReward": assessment.risk_reward,
            "passesMinRiskReward": assessment.passes_min_rr,
            "targetBasis": assessment.target_basis,
        }
    )


async def _position_sizing_handler(input_data: dict, context: ToolContext) -> ToolResult:
    entry = float(input_data["entry"])
    stop_loss = float(input_data["stopLoss"])
    settings = get_settings().risk
    equity = float(input_data.get("accountEquity", settings.account_equity))
    max_risk_pct = float(input_data.get("maxRiskPerTradePct", settings.max_risk_per_trade_pct))
    lot_size = int(input_data.get("lotSize", 1))

    position = risk_engine.size_position(
        entry=entry,
        stop_loss=stop_loss,
        settings=settings,
        equity=equity,
        max_risk_pct=max_risk_pct,
        lot_size=lot_size,
    )
    return ToolResult(
        output={
            "quantity": position.quantity,
            "notional": position.notional,
            "riskAmount": position.risk_amount,
            "riskPctOfEquity": position.risk_pct_of_equity,
            "rejectedReason": position.rejected_reason,
        }
    )


RISK_CALCULATOR_TOOL = ToolDefinition(
    name="risk_calculator",
    description="Pure-function risk assessment: given a direction, entry, and ATR, computes stop-loss, targets, and R:R.",
    input_schema={
        "type": "object",
        "required": ["direction", "entry", "atr"],
        "properties": {
            "direction": {"type": "string", "enum": ["LONG", "SHORT"]},
            "entry": {"type": "number", "exclusiveMinimum": 0},
            "atr": {"type": "number", "exclusiveMinimum": 0},
            "minRiskReward": {"type": "number", "exclusiveMinimum": 0},
            "atrStopMultiple": {"type": "number", "exclusiveMinimum": 0},
        },
    },
    output_schema={
        "type": "object",
        "required": ["direction", "entry", "stopLoss", "targets", "riskReward", "passesMinRiskReward"],
        "properties": {
            "direction": {"type": "string"},
            "entry": {"type": "number"},
            "stopLoss": {"type": "number"},
            "targets": {"type": "array"},
            "riskReward": {"type": "number"},
            "passesMinRiskReward": {"type": "boolean"},
            "targetBasis": {"type": "string"},
        },
    },
    handler=_risk_calculator_handler,
    kind="read_only",
    timeout_seconds=2.0,
    rate_limit_per_minute=600,  # pure computation — no external resource to protect
)

POSITION_SIZING_TOOL = ToolDefinition(
    name="position_sizing_calculator",
    description="Pure-function position sizing: given entry/stop and an account's risk budget, computes tradeable quantity.",
    input_schema={
        "type": "object",
        "required": ["entry", "stopLoss"],
        "properties": {
            "entry": {"type": "number", "exclusiveMinimum": 0},
            "stopLoss": {"type": "number", "exclusiveMinimum": 0},
            "accountEquity": {"type": "number", "exclusiveMinimum": 0},
            "maxRiskPerTradePct": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
            "lotSize": {"type": "integer", "minimum": 1},
        },
    },
    output_schema={
        "type": "object",
        "required": ["quantity", "notional", "riskAmount", "riskPctOfEquity"],
        "properties": {
            "quantity": {"type": "integer"},
            "notional": {"type": "number"},
            "riskAmount": {"type": "number"},
            "riskPctOfEquity": {"type": "number"},
            "rejectedReason": {"type": ["string", "null"]},
        },
    },
    handler=_position_sizing_handler,
    kind="read_only",
    timeout_seconds=2.0,
    rate_limit_per_minute=600,
)