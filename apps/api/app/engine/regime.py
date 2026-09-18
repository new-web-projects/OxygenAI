"""
Regime awareness — Passage 4 §4 (G06b), recovered verbatim:

  "don't run every indicator on every request. A lightweight regime
  classifier (trending vs. ranging vs. high-volatility, via ADX + ATR
  percentile) selects which signal families are even relevant before the
  AI reasons over them."

The output is load-bearing, not decorative: `relevant_signal_families`
is what the prompt builder uses to decide which indicator families the
provider is even shown, which is the mechanism that answers "the system
should determine which signals are relevant".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..schemas import OHLCVBar
from .indicator_library import adx, atr_series

Regime = Literal["trending_up", "trending_down", "ranging", "high_volatility", "unknown"]

# Wilder's own threshold: ADX above 25 is a trend, below 20 is not.
ADX_TREND_THRESHOLD = 25.0
ADX_RANGE_THRESHOLD = 20.0
# An ATR in the top decile of its own recent history is a volatility
# regime regardless of direction — position sizing and stop distance
# both change meaning there.
ATR_HIGH_PERCENTILE = 0.90


@dataclass(frozen=True)
class RegimeAssessment:
    regime: Regime
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    atr_percentile: float | None
    relevant_signal_families: list[str]
    rationale: str


def percentile_rank(values: list[float], target: float) -> float | None:
    """Fraction of `values` at or below `target`. None on an empty series."""
    if not values:
        return None
    at_or_below = sum(1 for v in values if v <= target)
    return at_or_below / len(values)


def classify(bars: list[OHLCVBar], adx_period: int = 14, atr_period: int = 14) -> RegimeAssessment:
    """
    Classify the current regime.

    ATR's percentile is computed on ATR *as a percentage of close*
    (ATR / price), not on raw ATR. Raw ATR trends upward alongside a
    rising price purely because range scales with price level — on a
    strongly trending instrument, the most recent raw-ATR value is
    almost always the highest the series has ever seen, which would
    misclassify every sustained trend as "high volatility" near its own
    end, regardless of whether relative volatility (the thing this check
    actually cares about) changed at all. Normalizing by price first is
    the standard fix for this and is what lets a real uptrend be read as
    trending_up rather than high_volatility.

    Order matters otherwise: high volatility is checked first because it
    changes how every other signal should be read, and a strongly
    trending market can also be a high-volatility one. Reporting
    "trending" while relative ATR sits in its top decile would
    understate the risk the user is taking.
    """
    adx_result = adx(bars, adx_period)
    raw_atr_values = atr_series(bars, atr_period)
    closes = [b.close for b in bars]
    normalized_atr_values = [
        (atr_value / close)
        for atr_value, close in zip(raw_atr_values, closes)
        if atr_value is not None and close > 0
    ]
    atr_pct = (
        percentile_rank(normalized_atr_values, normalized_atr_values[-1])
        if len(normalized_atr_values) >= 2
        else None
    )

    if adx_result is None and atr_pct is None:
        return RegimeAssessment(
            regime="unknown",
            adx=None,
            plus_di=None,
            minus_di=None,
            atr_percentile=None,
            relevant_signal_families=["trend", "momentum"],
            rationale="Not enough history to classify the regime; defaulting to a minimal signal set.",
        )

    adx_value = adx_result.adx if adx_result else None
    plus_di = adx_result.plus_di if adx_result else None
    minus_di = adx_result.minus_di if adx_result else None

    if atr_pct is not None and atr_pct >= ATR_HIGH_PERCENTILE:
        return RegimeAssessment(
            regime="high_volatility",
            adx=adx_value,
            plus_di=plus_di,
            minus_di=minus_di,
            atr_percentile=atr_pct,
            relevant_signal_families=["volatility", "trend", "structure"],
            rationale=(
                f"ATR is in the {atr_pct:.0%} percentile of its own recent range — "
                "volatility dominates; mean-reversion and momentum signals are unreliable here."
            ),
        )

    if adx_value is not None and adx_value >= ADX_TREND_THRESHOLD:
        directional: Regime = (
            "trending_up" if (plus_di or 0) >= (minus_di or 0) else "trending_down"
        )
        return RegimeAssessment(
            regime=directional,
            adx=adx_value,
            plus_di=plus_di,
            minus_di=minus_di,
            atr_percentile=atr_pct,
            relevant_signal_families=["trend", "momentum", "structure"],
            rationale=(
                f"ADX {adx_value:.1f} is above {ADX_TREND_THRESHOLD:.0f} with "
                f"{'+DI' if directional == 'trending_up' else '-DI'} leading — a directional trend. "
                "Trend-following and momentum signals apply; range signals do not."
            ),
        )

    if adx_value is not None and adx_value <= ADX_RANGE_THRESHOLD:
        return RegimeAssessment(
            regime="ranging",
            adx=adx_value,
            plus_di=plus_di,
            minus_di=minus_di,
            atr_percentile=atr_pct,
            relevant_signal_families=["mean_reversion", "structure", "volatility"],
            rationale=(
                f"ADX {adx_value:.1f} is at or below {ADX_RANGE_THRESHOLD:.0f} — no directional "
                "trend. Support/resistance and mean-reversion signals apply; trend-following does not."
            ),
        )

    return RegimeAssessment(
        regime="unknown",
        adx=adx_value,
        plus_di=plus_di,
        minus_di=minus_di,
        atr_percentile=atr_pct,
        relevant_signal_families=["trend", "momentum", "structure"],
        rationale=(
            f"ADX {adx_value:.1f} sits between the range and trend thresholds — "
            "the regime is genuinely indeterminate rather than assumed."
            if adx_value is not None
            else "ADX is unavailable; the regime is indeterminate."
        ),
    )