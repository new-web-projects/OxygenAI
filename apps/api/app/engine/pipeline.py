"""
Deterministic Trading Analysis Engine pipeline — Passage 1 §6.

The blueprint's stage sequence, implemented in order:

  Market data -> validation -> normalization -> feature engineering ->
  technical indicators -> market structure -> trend detection ->
  volatility analysis -> momentum analysis -> signal detection ->
  strategy evaluation -> risk engine -> (hand off to AI reasoning) ->
  verification -> final TradeAnalysis

Everything up to the hand-off happens here. Verification is in
`app/verify.py`, which runs after the AI layer returns.

Indicator computation routes through `native_bridge`, so a workload
promoted to C++ under §5.3 changes nothing at this call site — the
Python-facing signature is identical either way (§5.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import RiskSettings, get_settings
from ..observability import get_logger
from ..schemas import (
    BreakoutInfo,
    IndicatorBundle,
    LevelInfo,
    OHLCVBar,
    RegimeInfo,
    StructureInfo,
)
from . import indicator_library as lib
from . import market_structure, regime
from .native_bridge import compute_atr, compute_rsi, compute_sma

logger = get_logger("engine.pipeline")


class MarketDataError(ValueError):
    """Raised when input bars cannot support any analysis at all."""


@dataclass
class EngineOutput:
    indicators: IndicatorBundle
    regime: RegimeInfo
    structure: StructureInfo
    risk_settings: RiskSettings
    warnings: list[str]


# ---------------------------------------------------------------------------
# Stage 1-2: validation and normalization
# ---------------------------------------------------------------------------


def validate_bars(bars: list[OHLCVBar]) -> list[str]:
    """
    Reject impossible bars rather than computing confidently over them.

    An OHLC bar where high < low, or where close sits outside [low, high],
    is corrupt data. Silently indicator-ing it produces numbers that look
    authoritative and are meaningless — the exact failure mode this
    platform exists to avoid.
    """
    if not bars:
        raise MarketDataError("No market data bars were supplied")

    warnings: list[str] = []
    for index, bar in enumerate(bars):
        if bar.high < bar.low:
            raise MarketDataError(f"Bar {index} has high ({bar.high}) below low ({bar.low})")
        if not (bar.low <= bar.close <= bar.high):
            raise MarketDataError(
                f"Bar {index} close ({bar.close}) lies outside its low/high range"
            )
        if not (bar.low <= bar.open <= bar.high):
            raise MarketDataError(
                f"Bar {index} open ({bar.open}) lies outside its low/high range"
            )
        if bar.volume < 0:
            raise MarketDataError(f"Bar {index} has negative volume")
        if bar.close <= 0:
            raise MarketDataError(f"Bar {index} has a non-positive close")

    timestamps = [b.timestamp for b in bars]
    if timestamps != sorted(timestamps):
        warnings.append("Bars were not in chronological order and have been sorted")
    if len(set(timestamps)) != len(timestamps):
        warnings.append("Duplicate bar timestamps were present in the input")
    return warnings


def normalize_bars(bars: list[OHLCVBar]) -> list[OHLCVBar]:
    """Sort chronologically and drop duplicate timestamps, keeping the last."""
    by_timestamp: dict[str, OHLCVBar] = {}
    for bar in bars:
        by_timestamp[bar.timestamp] = bar
    return sorted(by_timestamp.values(), key=lambda b: b.timestamp)


# ---------------------------------------------------------------------------
# Stage 3-5: indicators
# ---------------------------------------------------------------------------


def compute_indicator_bundle(bars: list[OHLCVBar]) -> IndicatorBundle:
    """
    The full library from Passage 4 §4 (G06).

    SMA/RSI/ATR route through the native bridge (promotable per §5.3);
    the rest call the Python library directly, since none of them has
    been profiled as a bottleneck and §5.3 gates promotion on evidence.
    """
    closes = [b.close for b in bars]

    sma20 = compute_sma(closes, 20)
    sma50 = compute_sma(closes, 50)
    rsi14 = compute_rsi(closes, 14)
    atr14 = compute_atr(bars, 14)

    macd_result = lib.macd(closes)
    stoch_result = lib.stochastic(bars)
    adx_result = lib.adx(bars)
    bb_result = lib.bollinger_bands(closes)

    trend = "flat"
    if sma20 is not None and sma50 is not None:
        # A 0.1% dead band: two moving averages differing by a rounding
        # error are not a trend.
        if sma20 > sma50 * 1.001:
            trend = "up"
        elif sma20 < sma50 * 0.999:
            trend = "down"

    return IndicatorBundle(
        sma20=sma20,
        sma50=sma50,
        rsi14=rsi14,
        atr14=atr14,
        lastClose=closes[-1],
        trend=trend,  # type: ignore[arg-type]
        ema20=lib.ema(closes, 20),
        ema50=lib.ema(closes, 50),
        macd=macd_result.macd if macd_result else None,
        macdSignal=macd_result.signal if macd_result else None,
        macdHistogram=macd_result.histogram if macd_result else None,
        stochasticK=stoch_result.k if stoch_result else None,
        stochasticD=stoch_result.d if stoch_result else None,
        adx=adx_result.adx if adx_result else None,
        plusDi=adx_result.plus_di if adx_result else None,
        minusDi=adx_result.minus_di if adx_result else None,
        bollingerUpper=bb_result.upper if bb_result else None,
        bollingerMiddle=bb_result.middle if bb_result else None,
        bollingerLower=bb_result.lower if bb_result else None,
        bollingerPercentB=bb_result.percent_b if bb_result else None,
        vwap=lib.vwap(bars, 20),
    )


# ---------------------------------------------------------------------------
# Stage 6-11: structure, regime, risk framing
# ---------------------------------------------------------------------------


def build_structure_info(bars: list[OHLCVBar]) -> StructureInfo:
    assessment = market_structure.analyze(bars)
    return StructureInfo(
        structureBias=assessment.structure_bias,
        nearestSupport=assessment.nearest_support,
        nearestResistance=assessment.nearest_resistance,
        support=[
            LevelInfo(price=lv.price, kind=lv.kind, touches=lv.touches, strength=lv.strength)
            for lv in assessment.support
        ],
        resistance=[
            LevelInfo(price=lv.price, kind=lv.kind, touches=lv.touches, strength=lv.strength)
            for lv in assessment.resistance
        ],
        breakout=(
            BreakoutInfo(
                direction=assessment.breakout.direction,
                level=assessment.breakout.level,
                close=assessment.breakout.close,
                volumeRatio=assessment.breakout.volume_ratio,
                confirmed=assessment.breakout.confirmed,
                reason=assessment.breakout.reason,
            )
            if assessment.breakout
            else None
        ),
    )


def build_regime_info(bars: list[OHLCVBar]) -> RegimeInfo:
    assessment = regime.classify(bars)
    return RegimeInfo(
        regime=assessment.regime,
        adx=assessment.adx,
        atrPercentile=assessment.atr_percentile,
        relevantSignalFamilies=list(assessment.relevant_signal_families),
        rationale=assessment.rationale,
    )


def run_engine(
    bars: list[OHLCVBar],
    *,
    account_equity: float | None = None,
    max_risk_per_trade_pct: float | None = None,
) -> EngineOutput:
    """Run the deterministic half of the pipeline, up to the AI hand-off."""
    warnings = validate_bars(bars)
    normalized = normalize_bars(bars)

    indicators = compute_indicator_bundle(normalized)
    structure = build_structure_info(normalized)
    regime_info = build_regime_info(normalized)

    base = get_settings().risk
    risk_settings = RiskSettings(
        account_equity=account_equity if account_equity is not None else base.account_equity,
        max_risk_per_trade_pct=(
            max_risk_per_trade_pct
            if max_risk_per_trade_pct is not None
            else base.max_risk_per_trade_pct
        ),
        sizing_method=base.sizing_method,
        atr_stop_multiple=base.atr_stop_multiple,
        min_risk_reward=base.min_risk_reward,
        target_r_multiples=base.target_r_multiples,
    )

    if indicators.atr14 is None:
        warnings.append(
            "ATR(14) is unavailable — not enough history to place a volatility-based stop."
        )
    if indicators.sma50 is None:
        warnings.append("SMA(50) is unavailable — trend classification is degraded.")

    return EngineOutput(
        indicators=indicators,
        regime=regime_info,
        structure=structure,
        risk_settings=risk_settings,
        warnings=warnings,
    )


def risk_context_dict(output: EngineOutput) -> dict[str, Any]:
    """The risk framing handed to the prompt builder (read-only for the AI)."""
    atr = output.indicators.atr14
    return {
        "riskPerUnit": (atr * output.risk_settings.atr_stop_multiple) if atr else None,
        "minRiskReward": output.risk_settings.min_risk_reward,
        "maxRiskPerTradePct": output.risk_settings.max_risk_per_trade_pct,
    }