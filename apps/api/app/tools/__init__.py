"""
The 15-tool registry - Passage 1 §4.8, recovered in full by Passage 4
§3.5 (G05).

- schemas.py    - the contract every tool satisfies (ToolDefinition,
                   ToolContext, ToolResult, ToolExecutionOutcome).
- rate_limiter.py - per-tool in-process token bucket.
- registry.py   - the governance engine: permission -> rate limit ->
                   input schema -> timeout -> execution -> output
                   schema -> DB logging. The one path every tool call
                   goes through.
- market_tools.py, risk_tools.py, data_tools.py, knowledge_tools.py,
  deferred_tools.py - the 15 tool definitions.
- builtin.py    - ALL_TOOLS, assembled from the five files above.
"""