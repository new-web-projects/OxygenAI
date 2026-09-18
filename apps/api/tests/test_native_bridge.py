"""
Native bridge tests — Passage 1 §5.5's two hard requirements, checked as
actual assertions rather than asserted informally in a docstring:

1. "Every promoted function is validated against its pre-promotion
   Python/numpy implementation ... numerical parity is a release gate."
2. "The C++ layer must not become a dependency the Python backend cannot
   run without ... degrades performance, never correctness."

These tests run whether or not the native extension is actually built on
the machine executing them — the parity checks skip cleanly (not fail)
when there is nothing to compare against, and the fallback tests confirm
correctness independent of the native layer either way.
"""

from __future__ import annotations

from app.engine import indicator_library as py_impl
from app.engine.native_bridge import check_parity, load_native, parity_ok


def test_parity_check_returns_empty_when_native_is_unavailable(monkeypatch):
    """No comparison to make is not the same as a parity failure."""
    import app.engine.native_bridge as native_bridge

    monkeypatch.setattr(native_bridge, "_module", None)
    monkeypatch.setattr(
        native_bridge, "_status", native_bridge.NativeStatus(False, "forced unavailable for test")
    )
    assert check_parity() == []
    assert parity_ok() is True  # vacuously true — nothing to disagree on


def test_parity_holds_within_tolerance_when_native_is_available():
    status = load_native()
    if not status.available:
        import pytest

        pytest.skip("native extension not built in this environment")
    results = check_parity(tolerance=1e-6)
    assert len(results) >= 4  # sma, ema, rsi, atr at minimum
    for result in results:
        assert result.within_tolerance, (
            f"{result.workload}: python={result.python_value} native={result.native_value} "
            f"diff={result.absolute_difference}"
        )


def test_compute_functions_match_pure_python_when_nothing_is_promoted(monkeypatch):
    """
    With NATIVE_PROMOTED_WORKLOADS unset (the correct default per
    Passage 1 §5.3 — nothing is promoted without profiling evidence),
    every compute_* wrapper must produce exactly what the pure-Python
    implementation produces, because that is what actually runs.
    """
    from app.engine.native_bridge import compute_ema, compute_rsi, compute_sma

    monkeypatch.delenv("NATIVE_PROMOTED_WORKLOADS", raising=False)
    closes = [100.0 + i * 0.7 for i in range(40)]

    assert compute_sma(closes, 20) == py_impl.sma(closes, 20)
    assert compute_ema(closes, 20) == py_impl.ema(closes, 20)
    assert compute_rsi(closes, 14) == py_impl.rsi_wilder(closes, 14)


def test_load_native_never_raises_regardless_of_environment():
    """The one hard invariant: this call must never throw, on any machine."""
    status = load_native(force=True)
    assert isinstance(status.available, bool)
    assert isinstance(status.reason, str)