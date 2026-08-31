import pytest

from app.comparison import run_comparison, validate_provider_set
from app.indicators import compute_indicators, generate_synthetic_ohlcv


def test_validate_provider_set_rejects_fewer_than_2():
    assert validate_provider_set(["mock"]) == "providers must list 2 or 3 provider ids"


def test_validate_provider_set_rejects_more_than_3():
    assert validate_provider_set(["mock", "custom", "grok", "gemma"]) == "providers must list 2 or 3 provider ids"


def test_validate_provider_set_rejects_a_repeat():
    assert validate_provider_set(["mock", "mock"]) == "providers must not repeat"


def test_validate_provider_set_rejects_unknown_id():
    err = validate_provider_set(["mock", "chatgpt"])
    assert err is not None and "unknown provider id" in err


def test_validate_provider_set_accepts_a_real_2_provider_combo():
    assert validate_provider_set(["grok", "gemma"]) is None


def test_validate_provider_set_accepts_the_full_three_way():
    assert validate_provider_set(["custom", "grok", "gemma"]) is None


@pytest.mark.asyncio
async def test_isolation_one_unconfigured_provider_does_not_take_down_the_others():
    bars = generate_synthetic_ohlcv("ISOTEST", 60)
    indicators = compute_indicators(bars)
    last_bars = [{"timestamp": b.timestamp, "close": b.close} for b in bars[-5:]]

    # grok and gemma have no keys configured in this environment -- both
    # slots should come back "unavailable", not raise and take mock down
    # with them.
    results = await run_comparison(
        ["mock", "grok", "gemma"], "ISOTEST", indicators, last_bars, lambda pid: f"{pid}-model"
    )

    assert len(results) == 3
    by_id = {r.providerId: r for r in results}
    assert by_id["mock"].outcome == "ok"
    assert by_id["grok"].outcome == "unavailable"
    assert by_id["gemma"].outcome == "unavailable"


@pytest.mark.asyncio
async def test_isolation_two_unconfigured_providers_each_get_independent_reasons():
    bars = generate_synthetic_ohlcv("ISOTEST2", 60)
    indicators = compute_indicators(bars)
    last_bars = [{"timestamp": b.timestamp, "close": b.close} for b in bars[-5:]]

    results = await run_comparison(["grok", "gemma"], "ISOTEST2", indicators, last_bars, lambda pid: f"{pid}-model")

    assert len(results) == 2
    for r in results:
        assert r.outcome == "unavailable"
        assert len(r.reason) > 0

    assert results[0].reason != results[1].reason
