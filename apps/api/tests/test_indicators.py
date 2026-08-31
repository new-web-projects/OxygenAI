from app.indicators import atr, compute_indicators, generate_synthetic_ohlcv, rsi, sma


def test_sma_returns_none_when_not_enough_data():
    assert sma([1, 2, 3], 5) is None


def test_sma_computes_a_plain_average():
    assert sma([1, 2, 3, 4, 5], 5) == 3


def test_rsi_is_100_when_no_losses_in_window():
    closes = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
    assert rsi(closes, 14) == 100


def test_rsi_stays_within_0_100_for_mixed_data():
    closes = [10, 11, 9, 12, 8, 13, 7, 14, 6, 15, 5, 16, 4, 17, 3]
    value = rsi(closes, 14)
    assert value is not None and 0 <= value <= 100


def test_atr_is_non_negative_on_synthetic_data():
    bars = generate_synthetic_ohlcv("TEST", 30)
    value = atr(bars, 14)
    assert value is not None and value >= 0


def _without_timestamp(bar) -> dict:
    d = bar.model_dump()
    d.pop("timestamp")
    return d


def test_generate_synthetic_ohlcv_is_deterministic_for_the_same_symbol():
    # Compares OHLCV only, not the timestamp -- the timestamp is anchored
    # to wall-clock "now" by design (bars run up to the moment of the
    # call), so it's expected to differ by microseconds between two
    # separate calls even when the price series itself is identical.
    a = generate_synthetic_ohlcv("RELIANCE", 10)
    b = generate_synthetic_ohlcv("RELIANCE", 10)
    assert [_without_timestamp(bar) for bar in a] == [_without_timestamp(bar) for bar in b]


def test_generate_synthetic_ohlcv_differs_across_symbols():
    a = generate_synthetic_ohlcv("RELIANCE", 10)
    b = generate_synthetic_ohlcv("TCS", 10)
    assert [_without_timestamp(bar) for bar in a] != [_without_timestamp(bar) for bar in b]


def test_compute_indicators_produces_a_full_bundle_with_enough_history():
    bars = generate_synthetic_ohlcv("TCS", 60)
    bundle = compute_indicators(bars)
    assert bundle.sma20 is not None
    assert bundle.sma50 is not None
    assert bundle.rsi14 is not None
    assert bundle.atr14 is not None
    assert bundle.trend in ("up", "down", "flat")


def test_compute_indicators_degrades_gracefully_on_too_little_history():
    bars = generate_synthetic_ohlcv("NEWLISTING", 5)
    bundle = compute_indicators(bars)
    assert bundle.sma20 is None
    assert bundle.sma50 is None
    assert bundle.trend == "flat"
