"""
Shared prompt builder.

Extended to carry the deterministic engine's full output, not only the
four base indicators it previously sent. Passage 1 §6's pipeline
computes market structure, regime and risk; a provider that never saw
them could not reason over them, which made most of the engine invisible
to the AI layer.

Three blueprint rules are enforced in the prompt text itself:

* Passage 4 §3.6 (G09e): "The reasoning_summary field — never the
  provider's raw chain-of-thought." The prompt asks explicitly for a
  conclusion summary, not internal deliberation. This is the CoT
  non-disclosure requirement that both §4.6 and §4.7 dropped.

* Passage 1 §1 / §6: the provider reasons over numbers it did not
  generate, and may legitimately answer "no setup". Both are stated as
  instructions rather than assumed.

* Passage 1 §2: no "guaranteed returns" language anywhere in the
  product — instructed at the prompt level, then enforced again at the
  output-validation layer.

Regime-awareness (Passage 4 §4) is applied here as well as computed:
only the signal families the regime classifier marked relevant are
included, which is the mechanism behind "don't run every indicator on
every request".
"""

from __future__ import annotations

from typing import Any

from .base import AnalysisContext


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _indicator_block(context: AnalysisContext) -> str:
    ind = context.indicators
    families = set(
        (context.regime or {}).get("relevantSignalFamilies")
        or ["trend", "momentum", "structure"]
    )
    lines = [
        f"Last close: {_fmt(ind.lastClose)}",
        f"Trend (SMA20 vs SMA50): {ind.trend}",
        f"SMA(20): {_fmt(ind.sma20)}    SMA(50): {_fmt(ind.sma50)}",
    ]
    if ind.ema20 is not None or ind.ema50 is not None:
        lines.append(f"EMA(20): {_fmt(ind.ema20)}    EMA(50): {_fmt(ind.ema50)}")
    if "momentum" in families or "mean_reversion" in families:
        lines.append(f"RSI(14): {_fmt(ind.rsi14)}")
        if ind.stochasticK is not None:
            lines.append(
                f"Stochastic %K: {_fmt(ind.stochasticK)}  %D: {_fmt(ind.stochasticD)}"
            )
        if ind.macd is not None:
            lines.append(
                f"MACD: {_fmt(ind.macd, 4)}  signal: {_fmt(ind.macdSignal, 4)}  "
                f"histogram: {_fmt(ind.macdHistogram, 4)}"
            )
    if "trend" in families and ind.adx is not None:
        lines.append(
            f"ADX(14): {_fmt(ind.adx)}  +DI: {_fmt(ind.plusDi)}  -DI: {_fmt(ind.minusDi)}"
        )
    if "volatility" in families or "mean_reversion" in families:
        lines.append(f"ATR(14): {_fmt(ind.atr14)}")
        if ind.bollingerUpper is not None:
            lines.append(
                f"Bollinger: upper {_fmt(ind.bollingerUpper)} / middle "
                f"{_fmt(ind.bollingerMiddle)} / lower {_fmt(ind.bollingerLower)} "
                f"(percent-B {_fmt(ind.bollingerPercentB, 3)})"
            )
    if ind.vwap is not None:
        lines.append(f"VWAP(20): {_fmt(ind.vwap)}")
    return "\n".join(lines)


def _structure_block(context: AnalysisContext) -> str:
    structure = context.structure or {}
    if not structure:
        return ""
    parts = [f"Structure bias: {structure.get('structureBias', 'unknown')}"]
    if structure.get("nearestSupport") is not None:
        parts.append(f"Nearest support: {_fmt(structure['nearestSupport'])}")
    if structure.get("nearestResistance") is not None:
        parts.append(f"Nearest resistance: {_fmt(structure['nearestResistance'])}")
    breakout = structure.get("breakout")
    if breakout:
        state = "confirmed" if breakout.get("confirmed") else "UNCONFIRMED"
        parts.append(
            f"Breakout: {breakout.get('direction')} through "
            f"{_fmt(breakout.get('level'))} on "
            f"{_fmt(breakout.get('volumeRatio'))}x average volume ({state})"
        )
    return "\n".join(parts)


def _risk_block(context: AnalysisContext) -> str:
    risk = context.risk or {}
    if not risk:
        return ""
    return (
        "Risk framework (computed by the engine - do not restate these as your own "
        "numbers):\n"
        f"  ATR-based stop distance: {_fmt(risk.get('riskPerUnit'))} per unit\n"
        f"  Minimum acceptable R:R: {_fmt(risk.get('minRiskReward'))}\n"
        f"  Max risk per trade: {_fmt(risk.get('maxRiskPerTradePct'))}% of equity"
    )


def _knowledge_block(context: AnalysisContext) -> str:
    """Labelled RAG context - Passage 1 §7's source-type tag survives into the prompt."""
    if not context.knowledge:
        return ""
    lines = ["Retrieved context (each item is labelled with where it came from):"]
    for item in context.knowledge[:8]:
        source_type = item.get("sourceType", "knowledge")
        title = item.get("title") or item.get("documentTitle") or "untitled"
        content = (item.get("content") or "").strip().replace("\n", " ")
        if len(content) > 400:
            content = content[:400] + "..."
        lines.append(f"  [{source_type}] ({title}) {content}")
    return "\n".join(lines)


def build_prompt(context: AnalysisContext) -> str:
    regime = context.regime or {}
    sections = [
        "You are a trading analysis reasoner for a decision-support research tool.",
        "",
        "Hard rules:",
        "1. Every number below was computed deterministically by a calculation engine "
        "outside of you. Never invent, recompute, or adjust a number.",
        "2. You do not set entry, stop-loss or target prices. The engine does. You "
        "propose only a direction and a confidence, and explain your reasoning.",
        "3. Answering that there is no valid setup is a correct, expected outcome. "
        "Prefer it over a weak setup.",
        "4. Report a conclusion summary, not your internal chain-of-thought or "
        "step-by-step deliberation.",
        "5. This is research and analysis. Never promise or imply guaranteed returns.",
        "",
        f"Instrument: {context.symbol}    Timeframe: {context.timeframe}",
    ]

    if regime:
        sections += [
            "",
            f"Market regime: {regime.get('regime', 'unknown')}",
            f"  {regime.get('rationale', '')}",
            f"  Relevant signal families: "
            f"{', '.join(regime.get('relevantSignalFamilies', []))}",
            "  Signals outside those families were deliberately not computed for this "
            "request - do not ask for them or assume their values.",
        ]

    sections += ["", "Deterministic indicator values:", _indicator_block(context)]

    structure_block = _structure_block(context)
    if structure_block:
        sections += ["", "Market structure:", structure_block]

    risk_block = _risk_block(context)
    if risk_block:
        sections += ["", risk_block]

    knowledge_block = _knowledge_block(context)
    if knowledge_block:
        sections += ["", knowledge_block]

    sections += [
        "",
        "Respond with a single JSON object and nothing else - no prose before or "
        "after, no markdown fences:",
        '{"direction": "LONG" | "SHORT" | null, "confidence": <number 0-100> | null, '
        '"reasoningSummary": "<string>", "supportingEvidence": ["<string>", ...], '
        '"contradictingEvidence": ["<string>", ...]}',
        "",
        "If there is no valid setup, set direction and confidence to null and explain "
        "why in reasoningSummary. Cite the specific indicator names and values you "
        "relied on in supportingEvidence, and anything that argues against your "
        "conclusion in contradictingEvidence.",
    ]
    return "\n".join(sections)