"""
Regime classifier and market-structure tests — Passage 4 §4 (G06b):
"a lightweight regime classifier (trending vs. ranging vs.
high-volatility, via ADX + ATR percentile) selects which signal families
are even relevant."
"""

from __future__ import annotations

from app.engine import market_structure, regime
from app.schemas import OHLCVBar


def _bar(ts: str, o: float, h: float, l: float, c: float, v: float = 10_000.0) -> OHLCVBar:
    return OHLCVBar(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


def _wiggle(seed: int) -> float:
    """Deterministic pseudo-noise in [0, 1) — no randomness dependency, but not constant."""
    return ((seed * 2654435761) % 1000) / 1000.0


def _trend_bars(n: int, step: float = 1.5) -> list[OHLCVBar]:
    """
    A clean uptrend with genuinely varying (not constant) daily range,
    scaled to price. A fixed +/-0.6 wick on every bar makes true range
    near-constant, which makes ATR tie at its own maximum by
    construction and trivially read as "top decile" regardless of real
    volatility — an artifact of the test data, not of the classifier.
    Scaling the wick by price and adding bar-to-bar variation avoids
    that degenerate case.
    """
    bars = []
    price = 100.0
    for i in range(n):
        price += step
        wick = price * (0.004 + 0.006 * _wiggle(i))
        bars.append(
            _bar(f"2026-01-{i%28+1:02d}T00:00:00Z", price - wick * 0.4, price + wick, price - wick, price)
        )
    return bars


def _flat_bars(n: int) -> list[OHLCVBar]:
    """A genuinely range-bound series: small, varying oscillation around one level."""
    bars = []
    for i in range(n):
        wobble = (0.25 + 0.15 * _wiggle(i)) * (1 if i % 2 == 0 else -1)
        c = 100.0 + wobble
        wick = 0.3 + 0.2 * _wiggle(i + 7)
        bars.append(
            _bar(f"2026-01-{i%28+1:02d}T00:00:00Z", c - wick * 0.5, c + wick, c - wick, c)
        )
    return bars


class TestRegimeClassifier:
    def test_unknown_on_insufficient_history(self):
        result = regime.classify(_trend_bars(5))
        assert result.regime == "unknown"

    def test_strong_uptrend_is_classified_as_trending_up(self):
        result = regime.classify(_trend_bars(60, step=2.0))
        assert result.regime == "trending_up"
        assert "trend" in result.relevant_signal_families

    def test_flat_choppy_series_is_ranging_or_unknown(self):
        result = regime.classify(_flat_bars(60))
        # A near-flat series should not be reported as a confident trend.
        assert result.regime in ("ranging", "unknown")

    def test_relevant_signal_families_are_never_empty(self):
        for bars in (_trend_bars(60), _flat_bars(60), _trend_bars(5)):
            result = regime.classify(bars)
            assert len(result.relevant_signal_families) > 0

    def test_percentile_rank_of_the_max_value_is_one(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert regime.percentile_rank(values, 5.0) == 1.0

    def test_percentile_rank_of_empty_series_is_none(self):
        assert regime.percentile_rank([], 1.0) is None

    def test_rationale_is_always_a_nonempty_string(self):
        for bars in (_trend_bars(60), _flat_bars(60)):
            assert len(regime.classify(bars).rationale) > 0


class TestMarketStructure:
    def test_swing_points_exclude_unconfirmed_trailing_bars(self):
        bars = _trend_bars(30)
        swings = market_structure.find_swing_points(bars, lookback=2)
        # No swing should be reported in the last `lookback` bars — those
        # aren't confirmed yet.
        assert all(s.index < len(bars) - 2 for s in swings)

    def test_cluster_levels_groups_nearby_swings(self):
        from app.engine.market_structure import SwingPoint

        swings = [
            SwingPoint(index=1, timestamp="a", price=100.0, kind="high"),
            SwingPoint(index=5, timestamp="b", price=100.3, kind="high"),  # within 0.5% of 100
            SwingPoint(index=9, timestamp="c", price=150.0, kind="high"),  # far away
        ]
        levels = market_structure.cluster_levels(swings, "high", tolerance_pct=0.5)
        touches = sorted(lv.touches for lv in levels)
        assert touches == [1, 2]

    def test_breakout_requires_volume_confirmation(self):
        # 20 flat bars at low volume, then a strong up-close on high volume.
        bars = [_bar(f"d{i}", 100.0, 101.0, 99.0, 100.0, v=1000.0) for i in range(20)]
        bars.append(_bar("d20", 100.0, 106.0, 99.5, 105.0, v=5000.0))
        resistance = [
            market_structure.Level(price=101.0, kind="resistance", touches=2, last_touch_index=5, strength=2.0)
        ]
        breakout = market_structure.detect_breakout(bars, support=[], resistance=resistance)
        assert breakout is not None
        assert breakout.direction == "up"
        assert breakout.confirmed is True

    def test_breakout_on_low_volume_is_reported_but_unconfirmed(self):
        bars = [_bar(f"d{i}", 100.0, 101.0, 99.0, 100.0, v=1000.0) for i in range(20)]
        bars.append(_bar("d20", 100.0, 102.0, 99.5, 101.5, v=1050.0))  # barely above average
        resistance = [
            market_structure.Level(price=101.0, kind="resistance", touches=2, last_touch_index=5, strength=2.0)
        ]
        breakout = market_structure.detect_breakout(bars, support=[], resistance=resistance)
        assert breakout is not None
        assert breakout.confirmed is False

    def test_analyze_returns_a_full_assessment_without_crashing_on_short_history(self):
        result = market_structure.analyze(_trend_bars(5))
        assert result.structure_bias in ("higher_highs", "lower_lows", "sideways", "unknown")

    def test_nearest_support_and_resistance_bracket_the_last_close(self):
        bars = _trend_bars(80, step=1.0)
        result = market_structure.analyze(bars)
        last_close = bars[-1].close
        if result.nearest_support is not None:
            assert result.nearest_support < last_close
        if result.nearest_resistance is not None:
            assert result.nearest_resistance > last_close