"""
Tests for the concrete 15 tools (`tools/builtin.py`) — as opposed to
`test_tool_registry.py`, which tests the governance engine itself
against small hand-built tools. Structural checks confirm the registry
matches Passage 4 §3.5 (G05)'s table; functional tests exercise the
tools that have real (not "not yet available") handlers.
"""

from __future__ import annotations

import pytest

from app.tools.builtin import ALL_TOOLS
from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolContext


@pytest.fixture
def registry() -> ToolRegistry:
    r = ToolRegistry()
    for tool in ALL_TOOLS:
        r.register(tool)
    return r


class TestRegistryShape:
    def test_exactly_fifteen_tools_matching_passage_4_g05(self):
        assert len(ALL_TOOLS) == 15

    def test_all_names_are_unique(self):
        assert len({t.name for t in ALL_TOOLS}) == 15

    def test_every_tool_has_a_non_empty_description(self):
        for tool in ALL_TOOLS:
            assert tool.description.strip()

    def test_every_tool_declares_a_positive_timeout_and_rate_limit(self):
        for tool in ALL_TOOLS:
            assert tool.timeout_seconds > 0
            assert tool.rate_limit_per_minute > 0

    def test_none_of_the_three_named_providers_are_excluded_from_any_tool(self):
        """
        Passage 1 §4.8: "the tool registry is provider-agnostic — any of
        Custom AI, Grok, or Gemma 4 can call the same registered tools."
        """
        for tool in ALL_TOOLS:
            for provider_id in ("custom", "grok", "gemma"):
                assert tool.is_allowed_for(provider_id)

    def test_input_and_output_schemas_are_well_formed_json_schema(self):
        import jsonschema

        checker = jsonschema.Draft7Validator
        for tool in ALL_TOOLS:
            checker.check_schema(tool.input_schema)
            checker.check_schema(tool.output_schema)


class TestMarketDataTool:
    @pytest.mark.asyncio
    async def test_returns_a_quote_for_any_symbol(self, registry):
        outcome = await registry.execute(
            "market_data", {"symbol": "unittest_md"}, ToolContext(provider_id="mock")
        )
        assert outcome.status == "ok"
        assert outcome.output["symbol"] == "UNITTEST_MD"
        assert outcome.output["lastClose"] > 0

    @pytest.mark.asyncio
    async def test_rejects_missing_symbol(self, registry):
        outcome = await registry.execute("market_data", {}, ToolContext(provider_id="mock"))
        assert outcome.status == "error"

    @pytest.mark.asyncio
    async def test_falls_back_to_ephemeral_data_with_no_database_configured(self, registry, monkeypatch):
        import app.tools.market_tools as market_tools_module

        monkeypatch.setattr(market_tools_module, "is_db_configured", lambda: False)
        outcome = await registry.execute(
            "market_data", {"symbol": "nodbtest"}, ToolContext(provider_id="mock")
        )
        assert outcome.status == "ok"
        assert outcome.output["source"] == "ephemeral_demo"


class TestHistoricalDataTool:
    @pytest.mark.asyncio
    async def test_returns_the_requested_number_of_bars(self, registry):
        outcome = await registry.execute(
            "historical_data",
            {"symbol": "unittest_hist", "bars": 15},
            ToolContext(provider_id="mock"),
        )
        assert outcome.status == "ok"
        assert len(outcome.output["bars"]) == 15

    @pytest.mark.asyncio
    async def test_caps_bars_at_250_even_if_more_is_requested(self, registry):
        outcome = await registry.execute(
            "historical_data",
            {"symbol": "unittest_hist2", "bars": 9000},
            ToolContext(provider_id="mock"),
        )
        # Schema itself rejects >250 before the handler ever runs.
        assert outcome.status == "error"


class TestRiskCalculatorTool:
    @pytest.mark.asyncio
    async def test_long_setup_computes_a_stop_below_entry(self, registry):
        outcome = await registry.execute(
            "risk_calculator",
            {"direction": "LONG", "entry": 100.0, "atr": 2.0},
            ToolContext(provider_id="mock"),
        )
        assert outcome.status == "ok"
        assert outcome.output["stopLoss"] < 100.0
        assert outcome.output["riskReward"] != 1.0  # the constant-1.0 regression, checked here too

    @pytest.mark.asyncio
    async def test_rejects_an_invalid_direction(self, registry):
        outcome = await registry.execute(
            "risk_calculator",
            {"direction": "SIDEWAYS", "entry": 100.0, "atr": 2.0},
            ToolContext(provider_id="mock"),
        )
        assert outcome.status == "error"


class TestPositionSizingTool:
    @pytest.mark.asyncio
    async def test_computes_a_positive_quantity_for_a_reasonable_budget(self, registry):
        outcome = await registry.execute(
            "position_sizing_calculator",
            {"entry": 100.0, "stopLoss": 95.0, "accountEquity": 100000.0, "maxRiskPerTradePct": 1.0},
            ToolContext(provider_id="mock"),
        )
        assert outcome.status == "ok"
        assert outcome.output["quantity"] > 0

    @pytest.mark.asyncio
    async def test_zero_risk_distance_is_rejected_by_the_tool_not_a_crash(self, registry):
        outcome = await registry.execute(
            "position_sizing_calculator",
            {"entry": 100.0, "stopLoss": 100.0},
            ToolContext(provider_id="mock"),
        )
        assert outcome.status == "ok"  # the call succeeds; the sizing itself reports zero
        assert outcome.output["quantity"] == 0
        assert outcome.output["rejectedReason"] is not None


class TestTechnicalIndicatorTool:
    @pytest.mark.asyncio
    async def test_reports_unavailable_with_no_database_configured(self, registry, monkeypatch):
        import app.tools.market_tools as market_tools_module

        monkeypatch.setattr(market_tools_module, "is_db_configured", lambda: False)
        outcome = await registry.execute(
            "technical_indicator", {"symbol": "nodbtest"}, ToolContext(provider_id="mock")
        )
        assert outcome.status == "ok"
        assert outcome.available is False


class TestDeferredTools:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name,payload",
        [
            ("news", {"symbol": "TCS"}),
            ("sentiment", {"symbol": "TCS"}),
            ("economic_calendar", {"symbol": "TCS"}),
            ("backtesting", {"strategyId": "x"}),
            ("options_chain_analyzer", {"symbol": "TCS"}),
            ("portfolio_analyzer", {}),
            ("strategy_evaluator", {"strategyId": "x"}),
        ],
    )
    async def test_deferred_tool_reports_unavailable_not_fabricated_data(self, registry, name, payload):
        outcome = await registry.execute(name, payload, ToolContext(provider_id="mock"))
        assert outcome.status == "ok"  # the call itself succeeds
        assert outcome.available is False  # but honestly reports it has nothing real to say