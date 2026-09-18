"""
Central configuration.

Fixes a real defect found before this pass: both READMEs instruct
`cp .env.example .env.local`, and `.gitignore` lists `.env.local`, but
nothing in the codebase ever read it. Keys placed there were silently
invisible and every provider reported "not set". `load_env_files()` is
called once at import time by `app.main`, so the documented setup flow
now actually works.

Precedence (highest first): real process environment > .env.local > .env.
An already-exported variable is never overwritten by a file, so
`XAI_API_KEY=... uvicorn ...` still wins over a stale file value.

No secret is ever hardcoded here. Every value is read from the
environment; the defaults present are non-secret operational values
(timeouts, thresholds, ports) that Passage 1 / Passage 4 specify.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# apps/api/
_SERVICE_ROOT = Path(__file__).resolve().parent.parent
# repo root
_REPO_ROOT = _SERVICE_ROOT.parent.parent

_ENV_FILENAMES = (".env.local", ".env")


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env_files() -> list[str]:
    """
    Load .env.local then .env from apps/api/ and the repo root, without
    clobbering anything already present in the real environment.
    Returns the list of files actually applied, for startup logging.
    """
    applied: list[str] = []
    for directory in (_SERVICE_ROOT, _REPO_ROOT):
        for name in _ENV_FILENAMES:
            path = directory / name
            if not path.is_file():
                continue
            loaded = _parse_env_file(path)
            if not loaded:
                continue
            for key, value in loaded.items():
                # Real environment always wins.
                if key not in os.environ:
                    os.environ[key] = value
            applied.append(str(path))
    return applied


def _env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_csv(key: str, default: str) -> list[str]:
    raw = os.environ.get(key, default)
    # Whitespace is stripped per entry. Before this pass the CORS list was
    # split on "," with no strip(), so "a, b" produced " b", which never
    # matched an Origin header.
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class CircuitBreakerSettings:
    """Passage 4 §3.3 (G03): 5 failures / 60s, admin-configurable."""

    failure_threshold: int = field(default_factory=lambda: _env_int("CB_FAILURE_THRESHOLD", 5))
    window_seconds: float = field(default_factory=lambda: _env_float("CB_WINDOW_SECONDS", 60.0))
    cooldown_seconds: float = field(default_factory=lambda: _env_float("CB_COOLDOWN_SECONDS", 60.0))


@dataclass(frozen=True)
class RiskSettings:
    """
    Passage 4 §4 (G06) Risk Engine defaults. Every value is a policy
    input, not a market number — the engine never invents prices.
    """

    account_equity: float = field(default_factory=lambda: _env_float("RISK_ACCOUNT_EQUITY", 1_000_000.0))
    max_risk_per_trade_pct: float = field(
        default_factory=lambda: _env_float("RISK_MAX_PER_TRADE_PCT", 1.0)
    )
    sizing_method: str = field(default_factory=lambda: _env_str("RISK_SIZING_METHOD", "atr_based"))
    atr_stop_multiple: float = field(default_factory=lambda: _env_float("RISK_ATR_STOP_MULTIPLE", 1.5))
    # Passage 1 §6 / Passage 4 §4: R:R is computed, and a setup that
    # cannot clear the floor is suppressed rather than shipped.
    min_risk_reward: float = field(default_factory=lambda: _env_float("RISK_MIN_RR", 1.5))
    target_r_multiples: tuple[float, ...] = (2.0, 3.0, 4.0)


@dataclass(frozen=True)
class AuthSettings:
    jwt_secret: str = field(default_factory=lambda: _env_str("JWT_SECRET"))
    jwt_issuer: str = field(default_factory=lambda: _env_str("JWT_ISSUER", "oxygen-ai"))
    access_token_ttl_seconds: int = field(
        default_factory=lambda: _env_int("ACCESS_TOKEN_TTL_SECONDS", 3600)
    )
    # When no JWT_SECRET is configured the API refuses to mint tokens
    # rather than falling back to a baked-in development key.
    @property
    def is_configured(self) -> bool:
        return bool(self.jwt_secret)


@dataclass(frozen=True)
class RateLimitSettings:
    """Passage 4 §6.2 (G08b): per-plan limits, e.g. 30/min on analyze."""

    enabled: bool = field(default_factory=lambda: _env_bool("RATE_LIMIT_ENABLED", True))
    analyze_per_minute: int = field(default_factory=lambda: _env_int("RATE_LIMIT_ANALYZE", 30))
    default_per_minute: int = field(default_factory=lambda: _env_int("RATE_LIMIT_DEFAULT", 120))
    admin_per_minute: int = field(default_factory=lambda: _env_int("RATE_LIMIT_ADMIN", 60))


@dataclass(frozen=True)
class Settings:
    cors_allowed_origins: list[str] = field(
        default_factory=lambda: _env_csv("CORS_ALLOWED_ORIGINS", "http://localhost:3000")
    )
    database_url: str = field(default_factory=lambda: _env_str("DATABASE_URL"))
    redis_url: str = field(default_factory=lambda: _env_str("REDIS_URL"))
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO"))
    log_json: bool = field(default_factory=lambda: _env_bool("LOG_JSON", False))
    provider_timeout_seconds: float = field(
        default_factory=lambda: _env_float("PROVIDER_TIMEOUT_SECONDS", 30.0)
    )
    provider_retry_count: int = field(default_factory=lambda: _env_int("PROVIDER_RETRY_COUNT", 2))
    # Passage 1 §5: the native layer is always present as a boundary, but
    # a missing/unbuilt binary degrades performance, never correctness.
    native_enabled: bool = field(default_factory=lambda: _env_bool("NATIVE_ENABLED", True))

    circuit_breaker: CircuitBreakerSettings = field(default_factory=CircuitBreakerSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    rate_limit: RateLimitSettings = field(default_factory=RateLimitSettings)

    @property
    def database_configured(self) -> bool:
        return bool(self.database_url)

    @property
    def redis_configured(self) -> bool:
        return bool(self.redis_url)


_settings: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings