"""
Rule-based, not a model call — but it obeys the same law every provider
must obey: it only ever reasons over indicators.py's output, and it never
invents a price. Port of lib/providers/mockProvider.ts.
"""

from __future__ import annotations

from .base import AnalysisContext, ProviderReasoning


class MockProvider:
    id = "mock"
    display_name = "Mock Reasoner (offline)"

    def is_configured(self) -> bool:
        return True

    async def reason(self, context: AnalysisContext) -> ProviderReasoning:
        indicators = context.indicators
        trend = indicators.trend
        rsi14 = indicators.rsi14

        if trend == "flat" or rsi14 is None:
            return ProviderReasoning(
                direction=None,
                confidence=None,
                reasoning_summary=(
                    "Trend is flat and/or RSI has insufficient history — "
                    "no directional edge to report."
                ),
                supporting_evidence=[],
            )

        overbought = rsi14 > 70
        oversold = rsi14 < 30

        if trend == "up" and not overbought:
            return ProviderReasoning(
                direction="LONG",
                confidence=72 if oversold else 58,
                reasoning_summary=(
                    f"20-SMA is above 50-SMA (uptrend) and RSI(14) at {rsi14:.1f} "
                    "is not overbought, so momentum has room to continue."
                ),
                supporting_evidence=["sma20 > sma50", f"rsi14 = {rsi14:.1f} (not overbought)"],
            )

        if trend == "down" and not oversold:
            return ProviderReasoning(
                direction="SHORT",
                confidence=70 if overbought else 55,
                reasoning_summary=(
                    f"20-SMA is below 50-SMA (downtrend) and RSI(14) at {rsi14:.1f} "
                    "is not oversold, so downside momentum has room to continue."
                ),
                supporting_evidence=["sma20 < sma50", f"rsi14 = {rsi14:.1f} (not oversold)"],
            )

        return ProviderReasoning(
            direction=None,
            confidence=None,
            reasoning_summary=(
                "Trend direction is already extended on RSI (overbought in an "
                "uptrend, or oversold in a downtrend) — no valid setup."
            ),
            supporting_evidence=[],
        )


mock_provider = MockProvider()
