"""
Deterministic indicator engine. Port of lib/indicators.ts — same formulas,
same synthetic-data seeding scheme, so results are identical to what the
TypeScript version produced. This is Trading Analysis Engine territory
(Passage 1 §6 / Passage 4 §4): Python + numpy/pandas is the specified
baseline, never the AI layer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .schemas import IndicatorBundle, OHLCVBar
from .utils import round2


def sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    return sum(window) / period


def rsi(closes: list[float], period: int = 14) -> float | None:
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
    closes = [b.close for b in bars]
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    rsi14 = rsi(closes, 14)
    atr14 = atr(bars, 14)
    last_close = closes[-1]

    trend: str = "flat"
    if sma20 is not None and sma50 is not None:
        if sma20 > sma50 * 1.001:
            trend = "up"
        elif sma20 < sma50 * 0.999:
            trend = "down"

    return IndicatorBundle(
        sma20=sma20,
        sma50=sma50,
        rsi14=rsi14,
        atr14=atr14,
        lastClose=last_close,
        trend=trend,  # type: ignore[arg-type]
    )


def generate_synthetic_ohlcv(symbol: str, bars: int = 60) -> list[OHLCVBar]:
    """
    FOR DEMO ONLY. Deterministic pseudo-random walk seeded from the symbol
    string — not real market data, and not connected to a vendor. Ported
    bit-for-bit from indicators.ts's LCG so results match what the
    TypeScript version produced for the same symbol.
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
