"""
`validate_bars` must reject every non-finite OHLCV value (no database).

Before this, 4 of the 15 field/value combinations below were accepted:
high=+inf, low=-inf, volume=NaN, volume=+inf. They are latent with demo data
(always finite) but would turn real vendor data into non-finite indicators
and, from there, response fields that cannot be serialised as JSON.
"""

from __future__ import annotations

import pytest

from app.engine.pipeline import MarketDataError, validate_bars
from app.schemas import OHLCVBar


def _bar(index: int, **overrides) -> OHLCVBar:
    values = dict(
        timestamp=f"2026-01-{index + 1:02d}T00:00:00Z",
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1000.0,
    )
    values.update(overrides)
    return OHLCVBar(**values)


def test_clean_bars_pass_with_no_warnings():
    assert validate_bars([_bar(i) for i in range(20)]) == []


@pytest.mark.parametrize("field_name", ["open", "high", "low", "close", "volume"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "+inf", "-inf"])
def test_any_non_finite_value_is_rejected_with_the_offending_field_named(field_name, bad):
    bars = [_bar(i) for i in range(20)]
    bars[7] = _bar(7, **{field_name: bad})

    with pytest.raises(MarketDataError, match=rf"Bar 7 {field_name} .* not a finite number"):
        validate_bars(bars)
