// Native C++ test suite — Passage 1 §5.5:
//   "The C++ layer has its own unit-test suite ... run independently of
//    the Python test suite in CI, not only through the Python binding."
//
// Deliberately dependency-free rather than Catch2/GoogleTest. Both are
// reasonable choices per §5.5, but both want a network fetch at
// configure time, and a native suite that cannot run in an offline or
// air-gapped build is not actually an independent suite. The harness
// below is ~40 lines and gives the same failure reporting.

#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

#include "oxygen/indicators.hpp"

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool condition, const std::string& what) {
  ++g_checks;
  if (!condition) {
    ++g_failures;
    std::printf("  FAIL: %s\n", what.c_str());
  }
}

void check_close(double actual, double expected, double tolerance, const std::string& what) {
  ++g_checks;
  if (std::fabs(actual - expected) > tolerance) {
    ++g_failures;
    std::printf("  FAIL: %s (expected %.10f, got %.10f)\n", what.c_str(), expected, actual);
  }
}

void test_sma() {
  std::printf("test_sma\n");
  const std::vector<double> values{1, 2, 3, 4, 5};
  double out = 0.0;
  check(oxygen::sma(values.data(), values.size(), 5, &out), "sma over full window succeeds");
  check_close(out, 3.0, 1e-12, "sma([1..5], 5) == 3");

  check(oxygen::sma(values.data(), values.size(), 3, &out), "sma over trailing 3 succeeds");
  check_close(out, 4.0, 1e-12, "sma([1..5], 3) == 4");

  // Insufficient history must fail rather than fabricate a value.
  check(!oxygen::sma(values.data(), values.size(), 9, &out), "sma refuses too-short history");
  check(!oxygen::sma(values.data(), values.size(), 0, &out), "sma refuses period 0");
}

void test_ema() {
  std::printf("test_ema\n");
  // A constant series must give back the constant, whatever the period.
  const std::vector<double> flat(30, 42.0);
  double out = 0.0;
  check(oxygen::ema(flat.data(), flat.size(), 10, &out), "ema on constant series succeeds");
  check_close(out, 42.0, 1e-12, "ema of a constant series is that constant");

  const std::vector<double> ramp{1, 2, 3, 4, 5, 6, 7, 8, 9, 10};
  check(oxygen::ema(ramp.data(), ramp.size(), 3, &out), "ema on ramp succeeds");
  // Seed = mean(1,2,3) = 2; then k = 0.5 applied over values 4..10.
  double expected = 2.0;
  for (std::size_t i = 3; i < ramp.size(); ++i) expected += (ramp[i] - expected) * 0.5;
  check_close(out, expected, 1e-12, "ema matches the reference recurrence");
}

void test_rsi() {
  std::printf("test_rsi\n");
  // Monotonically rising closes have no losses -> RSI pinned at 100.
  std::vector<double> rising;
  for (int i = 0; i < 20; ++i) rising.push_back(10.0 + i);
  double out = 0.0;
  check(oxygen::rsi_wilder(rising.data(), rising.size(), 14, &out), "rsi on rising series succeeds");
  check_close(out, 100.0, 1e-9, "rsi of a monotonically rising series is 100");

  // Alternating series must stay inside [0, 100].
  std::vector<double> choppy;
  for (int i = 0; i < 40; ++i) choppy.push_back(i % 2 == 0 ? 100.0 : 101.0);
  check(oxygen::rsi_wilder(choppy.data(), choppy.size(), 14, &out), "rsi on choppy series succeeds");
  check(out >= 0.0 && out <= 100.0, "rsi stays within [0, 100]");

  check(!oxygen::rsi_wilder(rising.data(), 5, 14, &out), "rsi refuses too-short history");
}

void test_atr_and_true_range() {
  std::printf("test_atr_and_true_range\n");
  const std::size_t n = 30;
  std::vector<double> high(n), low(n), close(n);
  for (std::size_t i = 0; i < n; ++i) {
    close[i] = 100.0 + static_cast<double>(i);
    high[i] = close[i] + 1.0;
    low[i] = close[i] - 1.0;
  }
  std::vector<double> tr(n - 1);
  check(oxygen::true_range(high.data(), low.data(), close.data(), n, tr.data()),
        "true_range succeeds");
  // Each bar rises by 1, so TR = max(2, |high-prevClose|=2, |low-prevClose|=0) = 2.
  check_close(tr[10], 2.0, 1e-12, "true range on a steady +1 ramp is 2.0");

  double atr = 0.0;
  check(oxygen::atr_wilder(high.data(), low.data(), close.data(), n, 14, &atr),
        "atr succeeds");
  check_close(atr, 2.0, 1e-9, "atr of a constant-range series equals that range");
  check(!oxygen::atr_wilder(high.data(), low.data(), close.data(), 5, 14, &atr),
        "atr refuses too-short history");
}

void test_rolling_stddev() {
  std::printf("test_rolling_stddev\n");
  const std::vector<double> flat(10, 7.0);
  double out = 0.0;
  check(oxygen::rolling_stddev(flat.data(), flat.size(), 5, &out), "stddev succeeds");
  check_close(out, 0.0, 1e-12, "stddev of a constant series is 0");

  const std::vector<double> values{2, 4, 4, 4, 5, 5, 7, 9};
  check(oxygen::rolling_stddev(values.data(), values.size(), 8, &out), "stddev succeeds");
  // Population stddev of this classic set is exactly 2.
  check_close(out, 2.0, 1e-12, "population stddev matches the known reference value");
}

void test_sma_batch() {
  std::printf("test_sma_batch\n");
  const std::vector<double> values{1, 2, 3, 4, 5, 6, 7, 8, 9, 10};
  const auto results = oxygen::sma_batch(values, {5, 10, 50});
  check(results.size() == 3, "batch returns one result per period");
  check_close(results[0], 8.0, 1e-12, "batch sma period 5");
  check_close(results[1], 5.5, 1e-12, "batch sma period 10");
  check(std::isnan(results[2]), "batch sma yields NaN for an impossible period");
}

}  // namespace

int main() {
  std::printf("Oxygen AI native test suite (%s)\n\n", oxygen::build_info());
  test_sma();
  test_ema();
  test_rsi();
  test_atr_and_true_range();
  test_rolling_stddev();
  test_sma_batch();
  std::printf("\n%d checks, %d failures\n", g_checks, g_failures);
  return g_failures == 0 ? 0 : 1;
}