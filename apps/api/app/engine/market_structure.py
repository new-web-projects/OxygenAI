"""
Market structure, support/resistance, and breakout detection.

Passage 4 §4 (G06) is explicit that this is custom work, not a library
call: "custom modules for market-structure/support-resistance/breakout
detection (these aren't standard library functions — you'll write
swing-high/low + volume-confirmation logic yourselves)."

Implemented exactly as described: fractal swing points, level clustering
into support/resistance zones, and breakout detection that requires
volume confirmation rather than price alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..schemas import OHLCVBar

SwingKind = Literal["high", "low"]
LevelKind = Literal["support", "resistance"]


@dataclass(frozen=True)
class SwingPoint:
    index: int
    timestamp: str
    price: float
    kind: SwingKind


@dataclass(frozen=True)
class Level:
    """A clustered price zone, with strength = how many swings formed it."""

    price: float
    kind: LevelKind
    touches: int
    last_touch_index: int
    strength: float


@dataclass(frozen=True)
class Breakout:
    direction: Literal["up", "down"]
    level: float
    close: float
    volume_ratio: float
    confirmed: bool
    reason: str


@dataclass(frozen=True)
class StructureAssessment:
    swings: list[SwingPoint]
    support: list[Level]
    resistance: list[Level]
    nearest_support: float | None
    nearest_resistance: float | None
    breakout: Breakout | None
    structure_bias: Literal["higher_highs", "lower_lows", "sideways", "unknown"]


def find_swing_points(bars: list[OHLCVBar], lookback: int = 2) -> list[SwingPoint]:
    """
    Fractal swing detection: a swing high is a bar whose high exceeds
    every high within `lookback` bars either side.

    The last `lookback` bars are deliberately excluded — a swing point
    isn't confirmed until enough bars have formed after it. Including
    them would repaint, which is the classic way this indicator lies.
    """
    swings: list[SwingPoint] = []
    if len(bars) < lookback * 2 + 1:
        return swings
    for i in range(lookback, len(bars) - lookback):
        window = bars[i - lookback : i + lookback + 1]
        centre = bars[i]
        if centre.high == max(b.high for b in window) and any(
            b.high < centre.high for b in window
        ):
            swings.append(
                SwingPoint(index=i, timestamp=centre.timestamp, price=centre.high, kind="high")
            )
        elif centre.low == min(b.low for b in window) and any(b.low > centre.low for b in window):
            swings.append(
                SwingPoint(index=i, timestamp=centre.timestamp, price=centre.low, kind="low")
            )
    return swings


def cluster_levels(
    swings: list[SwingPoint], kind: SwingKind, tolerance_pct: float = 0.5
) -> list[Level]:
    """
    Group nearby swings of the same kind into levels. Two swings within
    `tolerance_pct` of each other are the same level — that's what makes
    a level "tested twice" rather than two separate levels.
    """
    points = [s for s in swings if s.kind == kind]
    if not points:
        return []
    points = sorted(points, key=lambda s: s.price)

    clusters: list[list[SwingPoint]] = [[points[0]]]
    for point in points[1:]:
        current = clusters[-1]
        anchor = sum(p.price for p in current) / len(current)
        if anchor > 0 and abs(point.price - anchor) / anchor * 100.0 <= tolerance_pct:
            current.append(point)
        else:
            clusters.append([point])

    level_kind: LevelKind = "resistance" if kind == "high" else "support"
    levels: list[Level] = []
    for cluster in clusters:
        price = sum(p.price for p in cluster) / len(cluster)
        last_index = max(p.index for p in cluster)
        levels.append(
            Level(
                price=price,
                kind=level_kind,
                touches=len(cluster),
                last_touch_index=last_index,
                # Strength favours levels touched often and recently;
                # a level last tested 200 bars ago is weaker evidence
                # than the same level tested last week.
                strength=float(len(cluster)) + last_index / 1000.0,
            )
        )
    return sorted(levels, key=lambda lv: lv.strength, reverse=True)


def detect_breakout(
    bars: list[OHLCVBar],
    support: list[Level],
    resistance: list[Level],
    volume_lookback: int = 20,
    volume_multiple: float = 1.5,
) -> Breakout | None:
    """
    Breakout detection with volume confirmation.

    A close beyond a level is a candidate; it is only *confirmed* when
    volume exceeds `volume_multiple` times its recent average. An
    unconfirmed candidate is still returned, flagged — suppressing it
    entirely would hide information the AI layer should reason over,
    while labelling it "confirmed" would overstate it.
    """
    if len(bars) < volume_lookback + 1:
        return None
    last = bars[-1]
    prior = bars[-(volume_lookback + 1) : -1]
    avg_volume = sum(b.volume for b in prior) / len(prior)
    volume_ratio = 0.0 if avg_volume <= 0 else last.volume / avg_volume
    confirmed = volume_ratio >= volume_multiple

    broken_resistance = [lv for lv in resistance if last.close > lv.price and bars[-2].close <= lv.price]
    if broken_resistance:
        level = max(broken_resistance, key=lambda lv: lv.strength)
        return Breakout(
            direction="up",
            level=level.price,
            close=last.close,
            volume_ratio=volume_ratio,
            confirmed=confirmed,
            reason=(
                f"Close {last.close:.2f} cleared resistance at {level.price:.2f} on "
                f"{volume_ratio:.2f}x average volume"
                + ("" if confirmed else " — below the confirmation threshold, treat as unconfirmed")
            ),
        )

    broken_support = [lv for lv in support if last.close < lv.price and bars[-2].close >= lv.price]
    if broken_support:
        level = max(broken_support, key=lambda lv: lv.strength)
        return Breakout(
            direction="down",
            level=level.price,
            close=last.close,
            volume_ratio=volume_ratio,
            confirmed=confirmed,
            reason=(
                f"Close {last.close:.2f} broke support at {level.price:.2f} on "
                f"{volume_ratio:.2f}x average volume"
                + ("" if confirmed else " — below the confirmation threshold, treat as unconfirmed")
            ),
        )
    return None


def _structure_bias(
    swings: list[SwingPoint],
) -> Literal["higher_highs", "lower_lows", "sideways", "unknown"]:
    highs = [s.price for s in swings if s.kind == "high"][-3:]
    lows = [s.price for s in swings if s.kind == "low"][-3:]
    if len(highs) < 2 or len(lows) < 2:
        return "unknown"
    rising_highs = all(b > a for a, b in zip(highs, highs[1:]))
    rising_lows = all(b > a for a, b in zip(lows, lows[1:]))
    falling_highs = all(b < a for a, b in zip(highs, highs[1:]))
    falling_lows = all(b < a for a, b in zip(lows, lows[1:]))
    if rising_highs and rising_lows:
        return "higher_highs"
    if falling_highs and falling_lows:
        return "lower_lows"
    return "sideways"


def analyze(
    bars: list[OHLCVBar], lookback: int = 2, max_levels: int = 3
) -> StructureAssessment:
    """Full structural read of the series."""
    swings = find_swing_points(bars, lookback)
    resistance = cluster_levels(swings, "high")[:max_levels]
    support = cluster_levels(swings, "low")[:max_levels]
    breakout = detect_breakout(bars, support, resistance) if bars else None

    last_close = bars[-1].close if bars else None
    nearest_support = None
    nearest_resistance = None
    if last_close is not None:
        below = [lv.price for lv in support if lv.price < last_close]
        above = [lv.price for lv in resistance if lv.price > last_close]
        nearest_support = max(below) if below else None
        nearest_resistance = min(above) if above else None

    return StructureAssessment(
        swings=swings,
        support=support,
        resistance=resistance,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        breakout=breakout,
        structure_bias=_structure_bias(swings),
    )