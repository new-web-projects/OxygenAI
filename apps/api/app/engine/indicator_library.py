"""
The full indicator library Passage 4 §4 (G06) names.

Passage 4's recovered text is explicit about the set: "RSI, MACD,
Stochastic, ATR, ADX, Bollinger Bands, VWAP, and standard moving
averages (SMA/EMA)". Before this pass only SMA, RSI and ATR existed.
The six missing families are implemented here.

Two deliberate correctness decisions, both departures from the previous
simple-average shortcuts:

1. RSI and ATR now use Wilder's smoothing, which is what RSI(14) and
   ATR(14) actually mean on every charting platform. The previous simple
   arithmetic mean over the last N periods produced numbers that would
   not match TradingView or any broker terminal for the same input — a
   silent accuracy defect in a product whose entire premise is
   "indicators are computed deterministically, never invented". The
   legacy simple-mean functions are kept in `app.indicators` and still
   exported, so nothing that depended on them breaks.

2. Every function returns `None` rather than a fabricated value when
   there is not enough history. Passage 1 §6's rule — "null field, not a
   fabricated number" — applies to the indicator layer, not only to the
   verification stage.

Pure functions over plain lists. No pandas dependency is introduced:
the working set is 60-250 daily bars per request, where pandas' overhead
exceeds its benefit, and staying on plain sequences keeps these
functions directly comparable against the native C++ implementations
(`native/`, Passage 1 §5.5's numerical-parity gate).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..schemas import OHLCVBar


# --------------------------------------------------------------------------
# Moving averages
# --------------------------------------------------------------------------


def sma_series(values: list[float], period: int) -> list[float | None]:
    """Simple moving average, aligned to the input (leading Nones)."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    running = sum(values[:period])
    out[period - 1] = running / period
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


def sma(values: list[float], period: int) -> float | None:
    series = sma_series(values, period)
    return series[-1] if series else None


def ema_series(values: list[float], period: int) -> list[float | None]:
    """
    Exponential moving average. Seeded with the SMA of the first `period`
    values — the standard seeding, and the one that makes EMA comparable
    across platforms.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    multiplier = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (values[i] - prev) * multiplier + prev
        out[i] = prev
    return out


def ema(values: list[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


# --------------------------------------------------------------------------
# Wilder-smoothed families
# --------------------------------------------------------------------------


def _wilder_smooth(values: list[float], period: int) -> list[float | None]:
    """
    Wilder's smoothing: seed with the arithmetic mean of the first
    `period` values, then prev + (current - prev) / period.
    """
    out: list[float | None] = [None] * len(values)
    if len(values) < period or period <= 0:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = prev + (values[i] - prev) / period
        out[i] = prev
    return out


def rsi_wilder(closes: list[float], period: int = 14) -> float | None:
    """
    RSI with Wilder's smoothing — the canonical definition.

    Returns None below `period + 1` closes. Returns 100.0 when there is
    no downward movement in the smoothed window (the standard limit, not
    a special case).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain_series = _wilder_smooth(gains, period)
    avg_loss_series = _wilder_smooth(losses, period)
    avg_gain = avg_gain_series[-1]
    avg_loss = avg_loss_series[-1]
    if avg_gain is None or avg_loss is None:
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_ranges(bars: list[OHLCVBar]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(bars)):
        cur = bars[i]
        prev_close = bars[i - 1].close
        out.append(
            max(
                cur.high - cur.low,
                abs(cur.high - prev_close),
                abs(cur.low - prev_close),
            )
        )
    return out


def atr_wilder(bars: list[OHLCVBar], period: int = 14) -> float | None:
    """ATR with Wilder's smoothing — the canonical definition."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(bars) < period + 1:
        return None
    smoothed = _wilder_smooth(true_ranges(bars), period)
    return smoothed[-1]


def atr_series(bars: list[OHLCVBar], period: int = 14) -> list[float | None]:
    """ATR aligned to `bars` (index 0 is always None — no prior close)."""
    if len(bars) < period + 1:
        return [None] * len(bars)
    smoothed = _wilder_smooth(true_ranges(bars), period)
    aligned: list[float | None] = [None]
    aligned.extend(smoothed)
    return aligned


# --------------------------------------------------------------------------
# MACD
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MACDResult:
    macd: float
    signal: float
    histogram: float


def macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal_period: int = 9
) -> MACDResult | None:
    """
    MACD line (fast EMA - slow EMA), its signal EMA, and the histogram.

    The signal EMA is computed over the MACD line's own defined region
    only — feeding it the leading Nones would shift it by `slow - 1`
    periods against every charting platform.
    """
    if fast >= slow:
        raise ValueError("fast period must be shorter than slow period")
    fast_series = ema_series(closes, fast)
    slow_series = ema_series(closes, slow)
    macd_line: list[float] = []
    for f, s in zip(fast_series, slow_series):
        if f is None or s is None:
            continue
        macd_line.append(f - s)
    if len(macd_line) < signal_period:
        return None
    signal_series = ema_series(macd_line, signal_period)
    signal_value = signal_series[-1]
    if signal_value is None:
        return None
    macd_value = macd_line[-1]
    return MACDResult(
        macd=macd_value, signal=signal_value, histogram=macd_value - signal_value
    )


# --------------------------------------------------------------------------
# Stochastic oscillator
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StochasticResult:
    k: float
    d: float


def stochastic(
    bars: list[OHLCVBar], k_period: int = 14, d_period: int = 3, smooth_k: int = 3
) -> StochasticResult | None:
    """
    Slow stochastic. %K is the smoothed raw %K; %D is the SMA of %K.
    A flat window (high == low) yields 50.0 — the neutral midpoint,
    which is the conventional treatment and avoids a divide-by-zero.
    """
    if len(bars) < k_period:
        return None
    raw_k: list[float] = []
    for i in range(k_period - 1, len(bars)):
        window = bars[i - k_period + 1 : i + 1]
        highest = max(b.high for b in window)
        lowest = min(b.low for b in window)
        span = highest - lowest
        if span == 0:
            raw_k.append(50.0)
        else:
            raw_k.append((bars[i].close - lowest) / span * 100.0)
    k_smoothed = [v for v in sma_series(raw_k, smooth_k) if v is not None]
    if len(k_smoothed) < d_period:
        return None
    d_series = [v for v in sma_series(k_smoothed, d_period) if v is not None]
    if not d_series:
        return None
    return StochasticResult(k=k_smoothed[-1], d=d_series[-1])


# --------------------------------------------------------------------------
# ADX / directional movement
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ADXResult:
    adx: float
    plus_di: float
    minus_di: float


def adx(bars: list[OHLCVBar], period: int = 14) -> ADXResult | None:
    """
    Average Directional Index with +DI / -DI, Wilder's method.

    Needs roughly 2 * period + 1 bars: one Wilder pass for the DIs and a
    second over the resulting DX series for ADX itself.
    """
    if len(bars) < 2 * period + 1:
        return None
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(bars)):
        up_move = bars[i].high - bars[i - 1].high
        down_move = bars[i - 1].low - bars[i].low
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    tr = true_ranges(bars)
    tr_s = _wilder_smooth(tr, period)
    plus_s = _wilder_smooth(plus_dm, period)
    minus_s = _wilder_smooth(minus_dm, period)

    dx_series: list[float] = []
    for t, p, m in zip(tr_s, plus_s, minus_s):
        if t is None or p is None or m is None or t == 0:
            continue
        plus_di = 100.0 * p / t
        minus_di = 100.0 * m / t
        di_sum = plus_di + minus_di
        dx_series.append(0.0 if di_sum == 0 else 100.0 * abs(plus_di - minus_di) / di_sum)

    if len(dx_series) < period:
        return None
    adx_series = _wilder_smooth(dx_series, period)
    adx_value = adx_series[-1]
    last_tr = tr_s[-1]
    last_plus = plus_s[-1]
    last_minus = minus_s[-1]
    if adx_value is None or last_tr is None or last_plus is None or last_minus is None or last_tr == 0:
        return None
    return ADXResult(
        adx=adx_value,
        plus_di=100.0 * last_plus / last_tr,
        minus_di=100.0 * last_minus / last_tr,
    )


# --------------------------------------------------------------------------
# Bollinger Bands
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BollingerResult:
    upper: float
    middle: float
    lower: float
    bandwidth: float
    percent_b: float | None


def bollinger_bands(
    closes: list[float], period: int = 20, std_multiple: float = 2.0
) -> BollingerResult | None:
    """
    Bollinger Bands using the population standard deviation (the
    convention Bollinger himself specifies, not the sample stdev).

    `percent_b` is None on a zero-width band rather than being forced to
    a number — a genuinely undefined value, per the no-fabrication rule.
    """
    if len(closes) < period:
        return None
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((v - middle) ** 2 for v in window) / period
    deviation = variance**0.5
    upper = middle + std_multiple * deviation
    lower = middle - std_multiple * deviation
    band_width = upper - lower
    percent_b = None if band_width == 0 else (closes[-1] - lower) / band_width
    return BollingerResult(
        upper=upper,
        middle=middle,
        lower=lower,
        bandwidth=0.0 if middle == 0 else band_width / middle,
        percent_b=percent_b,
    )


def bollinger_bandwidth_series(
    closes: list[float], period: int = 20, std_multiple: float = 2.0
) -> list[float | None]:
    """Bandwidth over time — the input to squeeze/volatility-regime detection."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        middle = sum(window) / period
        variance = sum((v - middle) ** 2 for v in window) / period
        deviation = variance**0.5
        out[i] = None if middle == 0 else (2 * std_multiple * deviation) / middle
    return out


# --------------------------------------------------------------------------
# VWAP
# --------------------------------------------------------------------------


def vwap(bars: list[OHLCVBar], period: int | None = None) -> float | None:
    """
    Volume-weighted average price over `period` bars (all bars when
    period is None). Typical price is (H+L+C)/3.

    On a daily timeframe this is a rolling VWAP, not an
    anchored-to-session VWAP — daily bars have no intraday session to
    anchor to. Intraday anchoring becomes meaningful only once a
    sub-daily timeframe exists; flagged rather than silently implied.
    """
    if not bars:
        return None
    window = bars if period is None else bars[-period:]
    if not window:
        return None
    total_volume = sum(b.volume for b in window)
    if total_volume <= 0:
        return None
    weighted = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in window)
    return weighted / total_volume