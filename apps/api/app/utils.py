from __future__ import annotations

from datetime import datetime, timedelta, timezone


def round2(n: float) -> float:
    return round(n * 100) / 100


STALE_THRESHOLD = timedelta(days=2)


def compute_freshness(last_bar_timestamp: str) -> bool:
    """
    True if the most recent bar is stale for a daily timeframe.

    The synthetic generator's last bar is always dated "yesterday"
    relative to generation time (by construction — see
    generate_synthetic_ohlcv), so a fresh generation is always ~1 day
    old the instant it's produced. That's the correct, expected age for
    a daily bar, not staleness. The threshold is 2 days specifically so
    a bar freshly generated a moment ago reads as current, and this only
    trips once a *further* full day passes with no new bar appended —
    e.g. persisted bars nobody has requested (and therefore refreshed)
    in several days.
    """
    ts = datetime.fromisoformat(last_bar_timestamp.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - ts) > STALE_THRESHOLD
