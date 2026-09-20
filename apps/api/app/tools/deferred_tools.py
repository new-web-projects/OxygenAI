"""
Seven of Passage 4 §3.5 (G05)'s 15 tools, all sharing one property: the
subsystem or vendor each one depends on doesn't exist in this codebase
yet, and three of them (News, Sentiment, Economic calendar) are named as
Post-MVP or vendor-licensed directly in the blueprint text itself:

  "News tool | Post-MVP, requires a licensed news feed"
  "Sentiment tool | Post-MVP, depends on the news tool"
  "Economic calendar tool | Upcoming macro/earnings events" (no vendor
    named or configured anywhere in this project)

Every one of the seven is registered with its full, real contract —
name, description, input/output schema, governance — so the registry's
count and shape match G05's table exactly. Every handler reports
`available: False` with a specific reason rather than fabricating a
plausible-looking result, which is the tool-level form of the same rule
the trading engine itself follows: a null field is honest, an invented
number is not.
"""

from __future__ import annotations

from .schemas import ToolContext, ToolDefinition, ToolResult


def _not_available(reason: str):
    async def _handler(input_data: dict, context: ToolContext) -> ToolResult:
        return ToolResult(output={}, available=False, unavailable_reason=reason)

    return _handler


_EMPTY_SCHEMA = {"type": "object"}
_AVAILABILITY_OUTPUT_SCHEMA = {"type": "object"}


NEWS_TOOL = ToolDefinition(
    name="news",
    description="Headline/article retrieval for an instrument.",
    input_schema={
        "type": "object",
        "required": ["symbol"],
        "properties": {"symbol": {"type": "string", "maxLength": 20}},
    },
    output_schema=_AVAILABILITY_OUTPUT_SCHEMA,
    handler=_not_available(
        "Post-MVP per Passage 1 §6/Passage 4 §4 — requires a licensed news feed, not configured."
    ),
    kind="read_only",
    availability_note="Post-MVP — requires a licensed news feed.",
)

SENTIMENT_TOOL = ToolDefinition(
    name="sentiment",
    description="Aggregated sentiment score for an instrument.",
    input_schema={
        "type": "object",
        "required": ["symbol"],
        "properties": {"symbol": {"type": "string", "maxLength": 20}},
    },
    output_schema=_AVAILABILITY_OUTPUT_SCHEMA,
    handler=_not_available("Post-MVP — depends on the News tool, which is itself not yet available."),
    kind="read_only",
    availability_note="Post-MVP — depends on the News tool.",
)

ECONOMIC_CALENDAR_TOOL = ToolDefinition(
    name="economic_calendar",
    description="Upcoming macro/earnings events for an instrument.",
    input_schema={
        "type": "object",
        "required": ["symbol"],
        "properties": {"symbol": {"type": "string", "maxLength": 20}},
    },
    output_schema=_AVAILABILITY_OUTPUT_SCHEMA,
    handler=_not_available("No economic-calendar data vendor is configured."),
    kind="read_only",
    availability_note="No vendor configured yet.",
)

BACKTESTING_TOOL = ToolDefinition(
    name="backtesting",
    description="Triggers a bounded backtest run for a named strategy (Passage 2 §18).",
    input_schema={
        "type": "object",
        "required": ["strategyId"],
        "properties": {
            "strategyId": {"type": "string"},
            "startDate": {"type": "string"},
            "endDate": {"type": "string"},
        },
    },
    output_schema=_AVAILABILITY_OUTPUT_SCHEMA,
    handler=_not_available("The backtesting engine (Passage 2 §18) is a later implementation phase."),
    kind="read_only",
    availability_note="Backtesting engine not yet implemented.",
)

OPTIONS_CHAIN_TOOL = ToolDefinition(
    name="options_chain_analyzer",
    description="Reads option-chain/Greeks data where the market-data vendor provides it.",
    input_schema={
        "type": "object",
        "required": ["symbol"],
        "properties": {"symbol": {"type": "string", "maxLength": 20}},
    },
    output_schema=_AVAILABILITY_OUTPUT_SCHEMA,
    handler=_not_available(
        "No options-chain data vendor is configured (Passage 4 §4 names TrueData/Global "
        "Datafeeds as candidates once F&O instruments are enabled)."
    ),
    kind="read_only",
    availability_note="No options-chain vendor configured yet.",
)

PORTFOLIO_ANALYZER_TOOL = ToolDefinition(
    name="portfolio_analyzer",
    description="Reads the acting user's own paper-trading/watchlist positions.",
    input_schema=_EMPTY_SCHEMA,
    output_schema=_AVAILABILITY_OUTPUT_SCHEMA,
    handler=_not_available("Paper trading / watchlists (Passage 2 §18) are a later implementation phase."),
    kind="mutating",  # would read user-scoped state once it exists — governed as mutating-tier from day one
    availability_note="Paper trading not yet implemented.",
)

STRATEGY_EVALUATOR_TOOL = ToolDefinition(
    name="strategy_evaluator",
    description="Scores a candidate setup against a named strategy's rules.",
    input_schema={
        "type": "object",
        "required": ["strategyId"],
        "properties": {"strategyId": {"type": "string"}},
    },
    output_schema=_AVAILABILITY_OUTPUT_SCHEMA,
    handler=_not_available(
        "No strategies are authored yet, and the custom-indicator expression evaluator "
        "(Passage 4 §3.1/G01b) this would build on is a later implementation phase."
    ),
    kind="read_only",
    availability_note="No strategies authored yet.",
)

DEFERRED_TOOLS = (
    NEWS_TOOL,
    SENTIMENT_TOOL,
    ECONOMIC_CALENDAR_TOOL,
    BACKTESTING_TOOL,
    OPTIONS_CHAIN_TOOL,
    PORTFOLIO_ANALYZER_TOOL,
    STRATEGY_EVALUATOR_TOOL,
)