"""
Assembles every registered tool into one list - the concrete 15 that
answer Passage 4 §3.5's table. `get_tool_registry()` in `registry.py`
imports `ALL_TOOLS` from here and registers each one exactly once.
"""

from __future__ import annotations

from .data_tools import DATABASE_SEARCH_TOOL
from .deferred_tools import DEFERRED_TOOLS
from .knowledge_tools import KNOWLEDGE_RETRIEVAL_TOOL
from .market_tools import (
    CHART_ANALYSIS_TOOL,
    HISTORICAL_DATA_TOOL,
    MARKET_DATA_TOOL,
    TECHNICAL_INDICATOR_TOOL,
)
from .risk_tools import POSITION_SIZING_TOOL, RISK_CALCULATOR_TOOL
from .schemas import ToolDefinition

ALL_TOOLS: tuple[ToolDefinition, ...] = (
    MARKET_DATA_TOOL,
    HISTORICAL_DATA_TOOL,
    TECHNICAL_INDICATOR_TOOL,
    CHART_ANALYSIS_TOOL,
    RISK_CALCULATOR_TOOL,
    POSITION_SIZING_TOOL,
    DATABASE_SEARCH_TOOL,
    KNOWLEDGE_RETRIEVAL_TOOL,
    *DEFERRED_TOOLS,
)

assert len(ALL_TOOLS) == 15, f"expected 15 tools per Passage 4 §3.5, found {len(ALL_TOOLS)}"
assert len({t.name for t in ALL_TOOLS}) == 15, "tool names must be unique"