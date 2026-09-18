#include "oxygen/indicators.hpp"

#include <cmath>
#include <stdexcept>

namespace oxygen {

bool sma(const double* values, std::size_t n, std::size_t period, double* out) {
  if (period == 0 || n < period || values == nullptr || out == nullptr) return false;
  double running = 0.0;
  for (std::size_t i = n - period; i < n; ++i) running += values[i];
  *out = running / static_cast<double>(period);
  return true;
}

bool ema(const double* values, std::size_t n, std::size_t period, double* out) {
  if (period == 0 || n < period || values == nullptr || out == nullptr) return false;
  double seed = 0.0;
  for (std::size_t i = 0; i < period; ++i) seed += values[i];
  double prev = seed / static_cast<double>(period);
  const double multiplier = 2.0 / (static_cast<double>(period) + 1.0);
  for (std::size_t i = period; i < n; ++i) {
    prev = (values[i] - prev) * multiplier + prev;
  }
  *out = prev;
  return true;
}

namespace {

// Wilder's smoothing, shared by RSI and ATR. Returns the final value.
bool wilder_final(const double* values, std::size_t n, std::size_t period, double* out) {
  if (period == 0 || n < period) return false;
  double seed = 0.0;
  for (std::size_t i = 0; i < period; ++i) seed += values[i];
  double prev = seed / static_cast<double>(period);
  for (std::size_t i = period; i < n; ++i) {
    prev = prev + (values[i] - prev) / static_cast<double>(period);
  }
  *out = prev;
  return true;
}

}  // namespace

bool rsi_wilder(const double* closes, std::size_t n, std::size_t period, double* out) {
  if (period == 0 || closes == nullptr || out == nullptr) return false;
  if (n < period + 1) return false;

  const std::size_t changes = n - 1;
  std::vector<double> gains(changes);
  std::vector<double> losses(changes);
  for (std::size_t i = 1; i < n; ++i) {
    const double change = closes[i] - closes[i - 1];
    gains[i - 1] = change > 0.0 ? change : 0.0;
    losses[i - 1] = change < 0.0 ? -change : 0.0;
  }

  double avg_gain = 0.0;
  double avg_loss = 0.0;
  if (!wilder_final(gains.data(), changes, period, &avg_gain)) return false;
  if (!wilder_final(losses.data(), changes, period, &avg_loss)) return false;

  if (avg_loss == 0.0) {
    *out = 100.0;
    return true;
  }
  const double rs = avg_gain / avg_loss;
  *out = 100.0 - 100.0 / (1.0 + rs);
  return true;
}

bool true_range(const double* high, const double* low, const double* close,
                std::size_t n, double* out) {
  if (n < 2 || high == nullptr || low == nullptr || close == nullptr || out == nullptr) {
    return false;
  }
  for (std::size_t i = 1; i < n; ++i) {
    const double prev_close = close[i - 1];
    const double a = high[i] - low[i];
    const double b = std::fabs(high[i] - prev_close);
    const double c = std::fabs(low[i] - prev_close);
    double tr = a;
    if (b > tr) tr = b;
    if (c > tr) tr = c;
    out[i - 1] = tr;
  }
  return true;
}

bool atr_wilder(const double* high, const double* low, const double* close,
                std::size_t n, std::size_t period, double* out) {
  if (period == 0 || n < period + 1) return false;
  std::vector<double> tr(n - 1);
  if (!true_range(high, low, close, n, tr.data())) return false;
  return wilder_final(tr.data(), tr.size(), period, out);
}

bool rolling_stddev(const double* values, std::size_t n, std::size_t period, double* out) {
  if (period == 0 || n < period || values == nullptr || out == nullptr) return false;
  double mean = 0.0;
  for (std::size_t i = n - period; i < n; ++i) mean += values[i];
  mean /= static_cast<double>(period);
  double variance = 0.0;
  for (std::size_t i = n - period; i < n; ++i) {
    const double d = values[i] - mean;
    variance += d * d;
  }
  variance /= static_cast<double>(period);
  *out = std::sqrt(variance);
  return true;
}

std::vector<double> sma_batch(const std::vector<double>& values,
                              const std::vector<std::size_t>& periods) {
  std::vector<double> results;
  results.reserve(periods.size());
  for (const std::size_t period : periods) {
    double value = 0.0;
    if (sma(values.data(), values.size(), period, &value)) {
      results.push_back(value);
    } else {
      results.push_back(std::nan(""));
    }
  }
  return results;
}

const char* build_info() {
#if defined(OXYGEN_CUDA_ENABLED)
  return "oxygen-native cpu+cuda";
#else
  return "oxygen-native cpu";
#endif
}

}  // namespace oxygen