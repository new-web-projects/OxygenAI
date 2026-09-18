"""
Indicator library tests — Passage 4 §4 (G06)'s named set: RSI, MACD,
Stochastic, ATR, ADX, Bollinger Bands, VWAP, SMA/EMA.

Reference values below are checked against hand-computable or
well-known cases (a flat series, a monotonic ramp, a known-stdev set)
rather than against another implementation, so a shared bug in both
implementations can't hide from the test.
"""

from __future__ import annotations

from app.engine import indicator_library as lib
from app.schemas import OHLCVBar


def _bar(ts: str, o: float, h: float, l: float, c: float, v: float = 1000.0) -> OHLCVBar:
    return OHLCVBar(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


def _ramp_bars(n: int, start: float = 100.0, step: float = 1.0) -> list[OHLCVBar]:
    bars = []
    for i in range(n):
        c = start + i * step
        bars.append(_bar(f"2026-01-{i+1:02d}T00:00:00Z", c - 0.4, c + 1.0, c - 1.0, c))
    return bars


def test_sma_matches_hand_computed_average():
    assert lib.sma([1, 2, 3, 4, 5], 5) == 3.0
    assert lib.sma([1, 2, 3, 4, 5], 3) == 4.0


def test_sma_returns_none_on_insufficient_history():
    assert lib.sma([1, 2], 5) is None


def test_ema_of_a_constant_series_is_that_constant():
    assert lib.ema([42.0] * 30, 10) == 42.0


def test_ema_seeds_with_sma_of_the_first_period():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    result = lib.ema(values, 3)
    expected = (values[0] + values[1] + values[2]) / 3.0
    multiplier = 2.0 / 4.0
    for v in values[3:]:
        expected = (v - expected) * multiplier + expected
    assert result == expected


def test_rsi_wilder_is_100_on_a_monotonically_rising_series():
    closes = [10.0 + i for i in range(20)]
    assert lib.rsi_wilder(closes, 14) == 100.0


def test_rsi_wilder_stays_within_bounds_on_mixed_data():
    closes = [100 + (i % 2) for i in range(40)]
    value = lib.rsi_wilder(closes, 14)
    assert value is not None
    assert 0.0 <= value <= 100.0


def test_rsi_wilder_none_on_insufficient_history():
    assert lib.rsi_wilder([1.0, 2.0, 3.0], 14) is None


def test_atr_wilder_on_constant_range_equals_that_range():
    bars = []
    for i in range(30):
        c = 100.0 + i
        bars.append(_bar(f"d{i}", c - 0.4, c + 1.0, c - 1.0, c))
    result = lib.atr_wilder(bars, 14)
    assert result is not None
    assert abs(result - 2.0) < 1e-6


def test_true_ranges_length_is_one_less_than_bars():
    bars = _ramp_bars(10)
    assert len(lib.true_ranges(bars)) == 9


def test_macd_returns_none_without_enough_history():
    assert lib.macd([1.0, 2.0, 3.0]) is None


def test_macd_histogram_equals_macd_minus_signal():
    closes = [100.0 + (i % 5) * 0.7 + i * 0.1 for i in range(60)]
    result = lib.macd(closes)
    assert result is not None
    assert abs(result.histogram - (result.macd - result.signal)) < 1e-9


def test_macd_rejects_fast_greater_than_or_equal_to_slow():
    import pytest

    with pytest.raises(ValueError):
        lib.macd([1.0] * 40, fast=26, slow=12)


def test_stochastic_flat_window_is_neutral_fifty():
    bars = [_bar(f"d{i}", 100.0, 100.0, 100.0, 100.0) for i in range(20)]
    result = lib.stochastic(bars)
    assert result is not None
    assert result.k == 50.0


def test_stochastic_none_on_insufficient_history():
    bars = _ramp_bars(5)
    assert lib.stochastic(bars) is None


def test_adx_none_on_insufficient_history():
    bars = _ramp_bars(10)
    assert lib.adx(bars, period=14) is None


def test_adx_on_a_clean_uptrend_shows_plus_di_leading():
    bars = _ramp_bars(60, start=100.0, step=1.0)
    result = lib.adx(bars, period=14)
    assert result is not None
    assert result.plus_di > result.minus_di


def test_bollinger_bands_use_population_stddev():
    # Classic textbook set with a known population stddev of 2.0.
    closes = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    result = lib.bollinger_bands(closes, period=8, std_multiple=2.0)
    assert result is not None
    assert abs((result.upper - result.middle) / 2.0 - 2.0) < 1e-9


def test_bollinger_percent_b_is_none_on_zero_width_band():
    result = lib.bollinger_bands([50.0] * 25, period=20, std_multiple=2.0)
    assert result is not None
    assert result.bandwidth == 0.0
    assert result.percent_b is None


def test_bollinger_none_on_insufficient_history():
    assert lib.bollinger_bands([1.0, 2.0], period=20) is None


def test_vwap_is_volume_weighted_not_a_plain_average():
    bars = [
        _bar("d1", 100.0, 101.0, 99.0, 100.0, v=10.0),
        _bar("d2", 100.0, 121.0, 119.0, 120.0, v=1000.0),
    ]
    result = lib.vwap(bars)
    assert result is not None
    # Heavily volume-weighted toward the second bar's ~120 typical price,
    # not the midpoint of 100 and 120.
    assert result > 115.0


def test_vwap_none_on_zero_total_volume():
    bars = [_bar("d1", 100.0, 101.0, 99.0, 100.0, v=0.0)]
    assert lib.vwap(bars) is None


def test_vwap_none_on_empty_input():
    assert lib.vwap([]) is None