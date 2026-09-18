"""
Backwards-compatible indicator surface.

The real implementations now live in `app/engine/` — the full library in
`engine/indicator_library.py`, orchestration in `engine/pipeline.py`, and
the promotable native path in `engine/native_bridge.py`.

This module is kept because several existing call sites and tests import
from it, and Passage-4-driven work is not a licence to break things that
already work. The legacy simple-mean `rsi`/`atr` are preserved verbatim
under their original names, while `compute_indicators` now returns the
full bundle.

Where the two differ: `rsi`/`atr` here are the original simple
arithmetic means over the trailing window; `engine.indicator_library`'s
`rsi_wilder`/`atr_wilder` are Wilder-smoothed, which is what RSI(14) and
ATR(14) mean on every charting platform. The bundle uses the Wilder
versions. Both are kept rather than silently swapped, so the difference
is visible instead of being a surprise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .engine.indicator_library import (  # noqa: F401 — re-exported for callers
    adx,
    atr_wilder,
    bollinger_bands,
    ema,
    macd,
    rsi_wilder,
    stochastic,
    vwap,
)
from .engine.pipeline import compute_indicator_bundle
from .schemas import IndicatorBundle, OHLCVBar
from .utils import round2


def sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    return sum(window) / period


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Legacy simple-mean RSI. See module docstring; prefer rsi_wilder."""
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains += change
        else:
            losses += abs(change)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def atr(bars: list[OHLCVBar], period: int = 14) -> float | None:
    """Legacy simple-mean ATR. See module docstring; prefer atr_wilder."""
    if len(bars) < period + 1:
        return None
    true_ranges: list[float] = []
    for i in range(len(bars) - period, len(bars)):
        cur = bars[i]
        prev_close = bars[i - 1].close
        tr = max(
            cur.high - cur.low,
            abs(cur.high - prev_close),
            abs(cur.low - prev_close),
        )
        true_ranges.append(tr)
    return sum(true_ranges) / period


def compute_indicators(bars: list[OHLCVBar]) -> IndicatorBundle:
    """Full bundle. Delegates to the engine pipeline."""
    return compute_indicator_bundle(bars)


def generate_synthetic_ohlcv(symbol: str, bars: int = 60) -> list[OHLCVBar]:
    """
    FOR DEMO ONLY. Deterministic pseudo-random walk seeded from the
    symbol string — not real market data and not connected to a vendor.

    Retained unchanged so existing determinism tests keep passing. The
    real market-data path is `app/market_data/`, which routes through a
    vendor adapter; this generator is the explicitly-labelled demo
    adapter behind that same interface (data_mode = 'demo', per the
    market_data schema column).
    """
    seed = 0
    for ch in symbol:
        seed = (seed * 31 + ord(ch)) % 100000
    if seed == 0:
        seed = 42

    def rand() -> float:
        nonlocal seed
        seed = (seed * 1103515245 + 12345) % 2147483648
        return seed / 2147483648

    result: list[OHLCVBar] = []
    price: float = float(100 + (seed % 400))
    now = datetime.now(timezone.utc)
    for i in range(bars, 0, -1):
        change = (rand() - 0.48) * price * 0.02
        open_ = price
        close = max(1.0, price + change)
        high = max(open_, close) + rand() * price * 0.005
        low = min(open_, close) - rand() * price * 0.005
        volume = int(10000 + rand() * 90000)
        ts = now - timedelta(days=i)
        result.append(
            OHLCVBar(
                timestamp=ts.isoformat().replace("+00:00", "Z"),
                open=round2(open_),
                high=round2(high),
                low=round2(low),
                close=round2(close),
                volume=volume,
            )
        )
        price = close
    return result