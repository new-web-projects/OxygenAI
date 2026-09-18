"""
Offline rule-based reasoner.

Not one of the blueprint's three providers - Passage 1 §2 names exactly
Custom AI, Grok and Gemma 4. Mock exists so the system is demonstrable
and testable without live credentials, and it obeys the same law every
provider obeys: it reasons only over the engine's output and never
invents a price.

Extended this pass to use the regime classifier and market structure the
engine now computes, so the demo path exercises the same enriched
context a real provider receives rather than a narrower one - otherwise
the mock would silently stop representing the real code path.
"""

from __future__ import annotations

from .base import AnalysisContext, HealthResult, ProviderReasoning


class MockProvider:
    id = "mock"
    display_name = "Mock Reasoner (offline)"
    supports_tools = False
    supports_local_runtime = True

    def is_configured(self) -> bool:
        return True

    def missing_configuration(self) -> list[str]:
        return []

    async def health_check(self) -> HealthResult:
        return HealthResult(
            ok=True,
            latency_ms=0.0,
            model_checked="mock-v1",
            detail="Offline reasoner; always available",
            transport="local_model",
        )

    async def reason(self, context: AnalysisContext) -> ProviderReasoning:
        indicators = context.indicators
        regime = (context.regime or {}).get("regime", "unknown")
        structure = context.structure or {}
        trend = indicators.trend
        rsi14 = indicators.rsi14

        evidence: list[str] = []
        contradicting: list[str] = []

        # Regime gating, mirroring what the classifier told the prompt.
        if regime == "high_volatility":
            return ProviderReasoning(
                direction=None,
                confidence=None,
                reasoning_summary=(
                    "ATR is in the top decile of its recent range. In a high-volatility "
                    "regime, stop distances widen faster than edge improves, so no "
                    "directional setup is proposed."
                ),
                supporting_evidence=[f"regime = {regime}"],
                contradicting_evidence=[],
            )

        if trend == "flat" or rsi14 is None:
            return ProviderReasoning(
                direction=None,
                confidence=None,
                reasoning_summary=(
                    "Trend is flat and/or RSI has insufficient history - no directional "
                    "edge to report."
                ),
                supporting_evidence=[],
                contradicting_evidence=[],
            )

        overbought = rsi14 > 70
        oversold = rsi14 < 30

        if indicators.adx is not None:
            if indicators.adx >= 25:
                evidence.append(f"adx = {indicators.adx:.1f} (directional trend present)")
            else:
                contradicting.append(
                    f"adx = {indicators.adx:.1f} is below 25 - trend strength is weak"
                )

        breakout = structure.get("breakout")
        if breakout and breakout.get("confirmed"):
            evidence.append(
                f"confirmed {breakout['direction']} breakout through "
                f"{breakout['level']:.2f} on {breakout['volumeRatio']:.2f}x volume"
            )
        elif breakout:
            contradicting.append("breakout is unconfirmed on volume")

        if trend == "up" and not overbought:
            evidence.insert(0, "sma20 > sma50")
            evidence.insert(1, f"rsi14 = {rsi14:.1f} (not overbought)")
            if structure.get("nearestResistance") is not None:
                contradicting.append(
                    f"resistance overhead at {structure['nearestResistance']:.2f}"
                )
            return ProviderReasoning(
                direction="LONG",
                confidence=72 if oversold else 58,
                reasoning_summary=(
                    f"20-SMA is above 50-SMA (uptrend) and RSI(14) at {rsi14:.1f} is not "
                    f"overbought, so momentum has room to continue. Regime: {regime}."
                ),
                supporting_evidence=evidence,
                contradicting_evidence=contradicting,
            )

        if trend == "down" and not oversold:
            evidence.insert(0, "sma20 < sma50")
            evidence.insert(1, f"rsi14 = {rsi14:.1f} (not oversold)")
            if structure.get("nearestSupport") is not None:
                contradicting.append(f"support below at {structure['nearestSupport']:.2f}")
            return ProviderReasoning(
                direction="SHORT",
                confidence=70 if overbought else 55,
                reasoning_summary=(
                    f"20-SMA is below 50-SMA (downtrend) and RSI(14) at {rsi14:.1f} is not "
                    f"oversold, so downside momentum has room to continue. Regime: {regime}."
                ),
                supporting_evidence=evidence,
                contradicting_evidence=contradicting,
            )

        return ProviderReasoning(
            direction=None,
            confidence=None,
            reasoning_summary=(
                "Trend direction is already extended on RSI (overbought in an uptrend, or "
                "oversold in a downtrend) - no valid setup."
            ),
            supporting_evidence=[],
            contradicting_evidence=[f"rsi14 = {rsi14:.1f} is at an extreme"],
        )


mock_provider = MockProvider()