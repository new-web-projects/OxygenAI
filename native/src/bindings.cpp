// pybind11 boundary — Passage 1 §5.4's recommended mechanism.
//
// Every function takes and returns NumPy-compatible buffers, never a
// Python object handed into C++ by reference. A promoted function's
// Python-facing signature is deliberately identical to its
// pre-promotion pure-Python equivalent, so promoting a workload never
// leaks implementation detail back into the call sites (§5.4's
// closing requirement).

#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>

#include <optional>

#include "oxygen/indicators.hpp"

namespace py = pybind11;

namespace {

using Array = py::array_t<double, py::array::c_style | py::array::forcecast>;

std::optional<double> py_sma(Array values, std::size_t period) {
  auto buf = values.request();
  double out = 0.0;
  if (!oxygen::sma(static_cast<const double*>(buf.ptr),
                   static_cast<std::size_t>(buf.size), period, &out)) {
    return std::nullopt;
  }
  return out;
}

std::optional<double> py_ema(Array values, std::size_t period) {
  auto buf = values.request();
  double out = 0.0;
  if (!oxygen::ema(static_cast<const double*>(buf.ptr),
                   static_cast<std::size_t>(buf.size), period, &out)) {
    return std::nullopt;
  }
  return out;
}

std::optional<double> py_rsi(Array closes, std::size_t period) {
  auto buf = closes.request();
  double out = 0.0;
  if (!oxygen::rsi_wilder(static_cast<const double*>(buf.ptr),
                          static_cast<std::size_t>(buf.size), period, &out)) {
    return std::nullopt;
  }
  return out;
}

std::optional<double> py_atr(Array high, Array low, Array close, std::size_t period) {
  auto hb = high.request();
  auto lb = low.request();
  auto cb = close.request();
  if (hb.size != lb.size || lb.size != cb.size) {
    throw std::invalid_argument("high/low/close arrays must be the same length");
  }
  double out = 0.0;
  if (!oxygen::atr_wilder(static_cast<const double*>(hb.ptr),
                          static_cast<const double*>(lb.ptr),
                          static_cast<const double*>(cb.ptr),
                          static_cast<std::size_t>(hb.size), period, &out)) {
    return std::nullopt;
  }
  return out;
}

std::optional<double> py_rolling_stddev(Array values, std::size_t period) {
  auto buf = values.request();
  double out = 0.0;
  if (!oxygen::rolling_stddev(static_cast<const double*>(buf.ptr),
                              static_cast<std::size_t>(buf.size), period, &out)) {
    return std::nullopt;
  }
  return out;
}

std::vector<double> py_sma_batch(const std::vector<double>& values,
                                 const std::vector<std::size_t>& periods) {
  return oxygen::sma_batch(values, periods);
}

}  // namespace

PYBIND11_MODULE(oxygen_native, m) {
  m.doc() =
      "Oxygen AI native performance layer (Passage 1 §5). "
      "Every function here has a pure-Python fallback in "
      "apps/api/app/engine/indicator_library.py; this module is an "
      "optimisation, never a correctness dependency.";

  m.attr("__version__") = "0.1.0";
  m.def("build_info", &oxygen::build_info,
        "Which native build answered — reported by the admin panel.");

  m.def("sma", &py_sma, py::arg("values"), py::arg("period"),
        "Simple moving average. Returns None when history is insufficient.");
  m.def("ema", &py_ema, py::arg("values"), py::arg("period"),
        "Exponential moving average, SMA-seeded.");
  m.def("rsi_wilder", &py_rsi, py::arg("closes"), py::arg("period"),
        "Wilder-smoothed RSI.");
  m.def("atr_wilder", &py_atr, py::arg("high"), py::arg("low"), py::arg("close"),
        py::arg("period"), "Wilder-smoothed ATR.");
  m.def("rolling_stddev", &py_rolling_stddev, py::arg("values"), py::arg("period"),
        "Population standard deviation over the trailing window.");
  m.def("sma_batch", &py_sma_batch, py::arg("values"), py::arg("periods"),
        "Batch SMA across many periods in one call.");
}