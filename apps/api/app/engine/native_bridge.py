"""
The Python side of the C++ boundary — Passage 1 §5.4 / §5.5.

Three rules from §5.5 are enforced here, not just documented:

1. "The C++ layer must not become a dependency the Python backend
   cannot run without." A missing, unbuilt, or ABI-mismatched extension
   degrades performance, never correctness: `load_native()` fails
   closed to the pure-Python implementation and the API starts normally.
   Confirmed by running the whole test suite with the extension both
   present and absent.

2. "Every promoted function is validated against its pre-promotion
   Python/numpy implementation on a fixed reference dataset before it
   replaces the Python path in production — numerical parity is a
   release gate, not a one-time check." `check_parity()` is that gate,
   and it runs in CI and is exposed through the admin API.

3. A promoted function's Python-facing signature stays identical to its
   pre-promotion equivalent. The `compute_*` wrappers below take and
   return exactly what `indicator_library` does.

No workload is promoted. Passage 1 §5.3 gates promotion on profiling
evidence against real load, and no real load exists yet, so every
`compute_*` call below still routes to Python unless
`NATIVE_PROMOTED_WORKLOADS` explicitly names it. The boundary exists so
that flipping one of those names on later is a config change, not a
redesign — which is exactly the distinction §5.2 draws between "the
layer exists" (mandatory now) and "which workloads run there" (decided
on evidence).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..observability import get_logger
from ..schemas import OHLCVBar
from . import indicator_library as py_impl

logger = get_logger("native")

_REPO_ROOT = Path(__file__).resolve().parents[4]
_NATIVE_BUILD_DIRS = [
    _REPO_ROOT / "native" / "build",
    _REPO_ROOT / "native" / "build" / "Release",
]

# Workloads promoted to the native layer. Empty by design — see the
# module docstring and Passage 1 §5.3. Set NATIVE_PROMOTED_WORKLOADS to
# a comma-separated list (e.g. "sma,rsi") only once profiling justifies
# it AND check_parity() passes for those names.
_PROMOTABLE = ("sma", "ema", "rsi", "atr", "stddev")


def _promoted_from_env() -> frozenset[str]:
    raw = os.environ.get("NATIVE_PROMOTED_WORKLOADS", "").strip()
    if not raw:
        return frozenset()
    requested = {p.strip().lower() for p in raw.split(",") if p.strip()}
    unknown = requested - set(_PROMOTABLE)
    if unknown:
        logger.warning(
            "ignoring unknown native workload names", extra={"unknown": sorted(unknown)}
        )
    return frozenset(requested & set(_PROMOTABLE))


@dataclass
class NativeStatus:
    available: bool
    reason: str
    build_info: str | None = None
    module_path: str | None = None
    promoted_workloads: list[str] = field(default_factory=list)


_module: Any | None = None
_status: NativeStatus | None = None


def load_native(force: bool = False) -> NativeStatus:
    """
    Attempt to load the compiled extension. Never raises.

    Import failure is an expected, supported state — the extension is
    platform-specific and may simply not have been built on this host.
    """
    global _module, _status
    if _status is not None and not force:
        return _status

    from ..config import get_settings

    if not get_settings().native_enabled:
        _module = None
        _status = NativeStatus(False, "Disabled by NATIVE_ENABLED=0")
        return _status

    for build_dir in _NATIVE_BUILD_DIRS:
        if build_dir.is_dir() and str(build_dir) not in sys.path:
            sys.path.insert(0, str(build_dir))

    try:
        import oxygen_native  # type: ignore[import-not-found]
    except ImportError as err:
        _module = None
        _status = NativeStatus(
            False,
            f"Native extension not built or not importable ({err}). "
            "Running on the pure-Python path — correctness is unaffected. "
            "Build it with: cmake -S native -B native/build && cmake --build native/build",
        )
        logger.info("native layer unavailable; using Python fallback", extra={"reason": str(err)})
        return _status

    try:
        build_info = oxygen_native.build_info()
        # Smoke test: the buffer protocol path depends on NumPy being
        # importable at call time, so a module that imports cleanly can
        # still be unusable. Proving it can actually execute one kernel
        # here means a later compute_* call never raises mid-request.
        smoke = oxygen_native.sma([1.0, 2.0, 3.0], 3)
        if smoke is None or abs(smoke - 2.0) > 1e-12:
            raise RuntimeError(f"native smoke test returned {smoke!r}, expected 2.0")
    except Exception as err:  # noqa: BLE001 — a broken build must not break startup
        _module = None
        _status = NativeStatus(
            False,
            f"Native extension loaded but unusable ({err}). "
            "Running on the pure-Python path — correctness is unaffected.",
        )
        logger.warning("native layer unusable; using Python fallback", extra={"reason": str(err)})
        return _status

    _module = oxygen_native
    _status = NativeStatus(
        available=True,
        reason="Native extension loaded",
        build_info=build_info,
        module_path=getattr(oxygen_native, "__file__", None),
        promoted_workloads=sorted(_promoted_from_env()),
    )
    logger.info(
        "native layer loaded",
        extra={"buildInfo": build_info, "promoted": _status.promoted_workloads},
    )
    return _status


def native_status() -> NativeStatus:
    return load_native()


def _use_native(workload: str) -> bool:
    status = load_native()
    return status.available and workload in _promoted_from_env()


def _as_float_list(values: list[float]) -> list[float]:
    return [float(v) for v in values]


# --------------------------------------------------------------------------
# Promotion-aware wrappers. Signatures are identical to the pure-Python
# functions they may one day replace (Passage 1 §5.4).
# --------------------------------------------------------------------------


def compute_sma(values: list[float], period: int) -> float | None:
    if _use_native("sma") and _module is not None:
        return _module.sma(_as_float_list(values), period)
    return py_impl.sma(values, period)


def compute_ema(values: list[float], period: int) -> float | None:
    if _use_native("ema") and _module is not None:
        return _module.ema(_as_float_list(values), period)
    return py_impl.ema(values, period)


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if _use_native("rsi") and _module is not None:
        return _module.rsi_wilder(_as_float_list(closes), period)
    return py_impl.rsi_wilder(closes, period)


def compute_atr(bars: list[OHLCVBar], period: int = 14) -> float | None:
    if _use_native("atr") and _module is not None:
        return _module.atr_wilder(
            [float(b.high) for b in bars],
            [float(b.low) for b in bars],
            [float(b.close) for b in bars],
            period,
        )
    return py_impl.atr_wilder(bars, period)


# --------------------------------------------------------------------------
# Numerical-parity gate (Passage 1 §5.5)
# --------------------------------------------------------------------------

# A fixed reference dataset, not a random one — §5.5 says "a fixed
# reference dataset", and a parity gate that changes its own inputs
# between runs cannot be a release gate.
_REFERENCE_CLOSES: list[float] = [
    100.0, 101.5, 99.8, 102.3, 103.1, 101.9, 104.4, 105.2, 103.7, 106.1,
    107.8, 106.4, 108.9, 110.2, 109.1, 111.5, 112.8, 110.9, 113.4, 115.0,
    114.2, 116.7, 118.1, 117.0, 119.3, 121.0, 119.8, 122.4, 124.1, 123.0,
    125.6, 127.2, 126.0, 128.5, 130.1, 129.0, 131.6, 133.2, 132.1, 134.7,
]


def _reference_bars() -> list[OHLCVBar]:
    bars: list[OHLCVBar] = []
    for i, close in enumerate(_REFERENCE_CLOSES):
        bars.append(
            OHLCVBar(
                timestamp=f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
                open=close - 0.4,
                high=close + 1.1,
                low=close - 1.3,
                close=close,
                volume=10_000.0 + i * 100,
            )
        )
    return bars


@dataclass(frozen=True)
class ParityResult:
    workload: str
    python_value: float | None
    native_value: float | None
    absolute_difference: float | None
    within_tolerance: bool
    note: str = ""


def check_parity(tolerance: float = 1e-9) -> list[ParityResult]:
    """
    Compare every native kernel against its pure-Python counterpart on
    the fixed reference dataset.

    Returns an empty list when the native layer is unavailable — nothing
    to compare is not the same as a parity failure, and conflating the
    two would make an unbuilt extension look like a numerical defect.
    """
    status = load_native()
    if not status.available or _module is None:
        return []

    bars = _reference_bars()
    closes = list(_REFERENCE_CLOSES)
    results: list[ParityResult] = []

    checks: list[tuple[str, Callable[[], float | None], Callable[[], float | None]]] = [
        ("sma", lambda: py_impl.sma(closes, 20), lambda: _module.sma(closes, 20)),
        ("ema", lambda: py_impl.ema(closes, 20), lambda: _module.ema(closes, 20)),
        (
            "rsi",
            lambda: py_impl.rsi_wilder(closes, 14),
            lambda: _module.rsi_wilder(closes, 14),
        ),
        (
            "atr",
            lambda: py_impl.atr_wilder(bars, 14),
            lambda: _module.atr_wilder(
                [b.high for b in bars], [b.low for b in bars], [b.close for b in bars], 14
            ),
        ),
    ]

    for name, py_fn, native_fn in checks:
        py_value = py_fn()
        native_value = native_fn()
        if py_value is None or native_value is None:
            results.append(
                ParityResult(
                    workload=name,
                    python_value=py_value,
                    native_value=native_value,
                    absolute_difference=None,
                    within_tolerance=py_value == native_value,
                    note="One side returned no value",
                )
            )
            continue
        difference = abs(py_value - native_value)
        results.append(
            ParityResult(
                workload=name,
                python_value=py_value,
                native_value=native_value,
                absolute_difference=difference,
                within_tolerance=difference <= tolerance,
            )
        )

    # Bollinger's stddev shares the same population-variance definition
    # on both sides; checked separately because its Python counterpart
    # lives inside bollinger_bands rather than as a standalone function.
    window = closes[-20:]
    mean = sum(window) / len(window)
    py_stddev = (sum((v - mean) ** 2 for v in window) / len(window)) ** 0.5
    native_stddev = _module.rolling_stddev(closes, 20)
    if native_stddev is not None:
        difference = abs(py_stddev - native_stddev)
        results.append(
            ParityResult(
                workload="stddev",
                python_value=py_stddev,
                native_value=native_stddev,
                absolute_difference=difference,
                within_tolerance=difference <= tolerance,
            )
        )

    return results


def parity_ok(tolerance: float = 1e-9) -> bool:
    """True when the native layer is absent (nothing to gate) or fully in parity."""
    results = check_parity(tolerance)
    return all(r.within_tolerance for r in results)