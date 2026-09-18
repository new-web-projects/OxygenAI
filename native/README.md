# Oxygen AI native performance layer

Passage 1 §5: a dedicated C++/CUDA architecture boundary that must exist
regardless of whether any workload currently runs there (§5.2's "the
layer exists" requirement, satisfied here). Which workloads actually run
here is a separate, profiling-gated decision (§5.3) — none are promoted
yet, correctly, since no real load has been profiled.

## What's here

- `include/oxygen/indicators.hpp` / `src/indicators.cpp` — the native
  kernels. No Python or pybind11 dependency, so this half is testable
  entirely on its own (§5.5).
- `src/bindings.cpp` — the pybind11 boundary (§5.4's recommended
  mechanism) exposing the kernels to Python as the `oxygen_native`
  module.
- `tests/test_indicators.cpp` — the independent C++ test suite (§5.5),
  runnable without Python or pybind11 at all.

## Building

Requires a C++17 compiler, CMake ≥ 3.18, and Python's `pybind11`
package (`pip install pybind11`, already in `apps/api/requirements.txt`).

```bash
cmake -S native -B native/build \
  -DPython3_EXECUTABLE="$(pwd)/apps/api/.venv/bin/python"
cmake --build native/build -j4
```

This produces `native/build/oxygen_native.cpython-*.so` (the Python
extension) and `native/build/oxygen_native_tests` (the C++ test binary).

If pybind11 isn't found, CMake prints a warning and skips the Python
extension — the build still succeeds, and the API still runs correctly
via the pure-Python fallback in `apps/api/app/engine/native_bridge.py`.
A missing native layer is a supported, tested state, not an error.

## Testing

```bash
# The independent C++ suite (§5.5) — no Python involved at all.
cd native/build && ctest --output-on-failure

# Or run the binary directly for verbose per-check output:
./native/build/oxygen_native_tests

# The numerical-parity release gate (§5.5) — every native kernel
# against its pure-Python counterpart on a fixed reference dataset:
cd apps/api && .venv/bin/python -c "
from app.engine.native_bridge import check_parity
for r in check_parity():
    print(r.workload, r.within_tolerance, r.absolute_difference)
"
```

## Enabling CUDA (only after a workload is actually promoted)

CUDA is off by default (`-DOXYGEN_ENABLE_CUDA=OFF`), matching §5.3: no
workload here is both parallelizable and large enough to justify GPU
dispatch overhead at this platform's current scale. To build with CUDA
support once that changes:

```bash
cmake -S native -B native/build -DOXYGEN_ENABLE_CUDA=ON
```

## Promoting a workload to native

Set `NATIVE_PROMOTED_WORKLOADS` (comma-separated: `sma,ema,rsi,atr,stddev`)
in the environment. `app/engine/native_bridge.py`'s `compute_*` wrappers
route to the native implementation only for names listed there — nothing
is promoted by default. Before promoting, run the parity check above and
confirm every listed workload reports `within_tolerance: True`; that
check is a release gate, not an optional step (§5.5).