from __future__ import annotations

from .base import AnalysisContext


def build_prompt(context: AnalysisContext) -> str:
    ind = context.indicators
    return f"""You are a trading analysis reasoner. You never invent numbers — you only reason over the indicators given below, which were computed deterministically outside of you.

Symbol: {context.symbol}
Last close: {ind.lastClose}
SMA(20): {ind.sma20 if ind.sma20 is not None else "n/a"}
SMA(50): {ind.sma50 if ind.sma50 is not None else "n/a"}
RSI(14): {ind.rsi14 if ind.rsi14 is not None else "n/a"}
ATR(14): {ind.atr14 if ind.atr14 is not None else "n/a"}
Trend: {ind.trend}

Respond ONLY with JSON matching exactly this shape, nothing else, no markdown fences:
{{"direction": "LONG" or "SHORT" or null, "confidence": number 0-100 or null, "reasoningSummary": string, "supportingEvidence": string[]}}

If there is no valid setup, set direction and confidence to null and explain why in reasoningSummary."""
