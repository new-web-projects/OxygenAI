from datetime import datetime, timedelta, timezone

from app.indicators import generate_synthetic_ohlcv
from app.utils import compute_freshness


def test_freshly_generated_bars_are_not_stale():
    # Regression test for a real bug: the synthetic generator's last bar
    # is always dated ~1 day before generation time by construction, so
    # a threshold of exactly 1 day tripped "stale" on every fresh
    # generation by a few microseconds of processing time.
    bars = generate_synthetic_ohlcv("FRESHTEST", 10)
    assert compute_freshness(bars[-1].timestamp) is False


def test_a_bar_several_days_old_is_stale():
    old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    assert compute_freshness(old_ts) is True


def test_a_bar_from_a_moment_ago_is_not_stale():
    fresh_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    assert compute_freshness(fresh_ts) is False
