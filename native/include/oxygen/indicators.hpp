// Oxygen AI — native indicator kernels.
//
// Passage 1 §5.4 defines the boundary shape precisely: "inputs and
// outputs are plain arrays/structs (NumPy-compatible buffers via
// pybind11's native NumPy support), never Python objects passed by
// reference into C++."
//
// This header therefore declares nothing but functions over
// const double* / size_t. It has no pybind11 include, no Python
// include, and no knowledge that Python exists — which is what lets
// the Catch2 suite in native/tests exercise it directly, per §5.5's
// requirement that the C++ layer be testable "independently of the
// Python test suite, not only through the Python binding."

#pragma once

#include <cstddef>
#include <vector>

namespace oxygen {

// Simple moving average over the trailing `period` values.
// Returns false when there is not enough history — the C++ analogue of
// the Python layer returning None rather than a fabricated number.
bool sma(const double* values, std::size_t n, std::size_t period, double* out);

// Exponential moving average, seeded with the SMA of the first
// `period` values (the same seeding the Python implementation uses —
// numerical parity is a release gate, §5.5).
bool ema(const double* values, std::size_t n, std::size_t period, double* out);

// Wilder-smoothed RSI. Returns 100.0 when the smoothed loss is zero.
bool rsi_wilder(const double* closes, std::size_t n, std::size_t period, double* out);

// True range series; writes n-1 values. Caller owns the buffer.
bool true_range(const double* high, const double* low, const double* close,
                std::size_t n, double* out);

// Wilder-smoothed ATR over OHLC arrays of length n.
bool atr_wilder(const double* high, const double* low, const double* close,
                std::size_t n, std::size_t period, double* out);

// Rolling population standard deviation over the trailing `period`.
bool rolling_stddev(const double* values, std::size_t n, std::size_t period, double* out);

// Batch SMA over many windows at once — the shape a promoted
// high-throughput workload (§5.3 row 4) would actually take.
std::vector<double> sma_batch(const std::vector<double>& values,
                              const std::vector<std::size_t>& periods);

// Build identification, surfaced through the Python binding so the
// admin panel can report which implementation actually answered.
const char* build_info();

}  // namespace oxygen