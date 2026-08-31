#!/usr/bin/env bash
# Migrates the backend to Python/FastAPI (Passage 1 §3).
# Run from the repo root: bash apply_fastapi_migration.sh
set -euo pipefail

echo "=== Removing files superseded by the Python backend ==="
rm -f app/api/analyze/route.ts
rm -f lib/comparison.test.ts
rm -f lib/comparison.ts
rm -f lib/db/client.ts
rm -f lib/db/comparisons.ts
rm -f lib/db/marketData.ts
rm -f lib/db/signals.ts
rm -f lib/indicators.test.ts
rm -f lib/indicators.ts
rm -f lib/providers/customAiProvider.ts
rm -f lib/providers/gemmaProvider.ts
rm -f lib/providers/grokProvider.ts
rm -f lib/providers/index.test.ts
rm -f lib/providers/index.ts
rm -f lib/providers/mockProvider.ts
rm -f lib/providers/types.ts
rm -f lib/scoring.test.ts
rm -f lib/scoring.ts
rm -f lib/verify.ts
rmdir lib/providers lib/db app/api/analyze app/api 2>/dev/null || true

echo "=== Creating directories ==="
mkdir -p apps/api/app/providers apps/api/app/db apps/api/app/routers apps/api/tests

echo "=== Writing new files ==="
echo '  new: apps/api/.env.example'
cat > apps/api/.env.example << 'OXYGEN_AI_FILE_EOF'
# CORS: the Next.js frontend's origin(s), comma-separated.
CORS_ALLOWED_ORIGINS=http://localhost:3000

# Optional. Without this, the app runs on ephemeral synthetic data --
# nothing persists, but it still works.
DATABASE_URL=

# Optional. Without these, the app falls back to the offline Mock
# provider for that provider id -- nothing breaks if you leave them blank.

# Gemma 4 -- hosted via Google's Gemini API transport (get a free key at
# https://aistudio.google.com/apikey). Deliberately not named GEMINI_*:
# Gemini is the transport, Gemma 4 is the provider.
GOOGLE_AI_API_KEY=
GEMMA_MODEL_ID=

# Grok -- from https://console.x.ai
XAI_API_KEY=
GROK_MODEL_ID=

# Custom AI ("Oxygen AI" in the UI) -- any OpenAI-compatible
# chat-completions endpoint. All three must be set together.
CUSTOM_AI_BASE_URL=
CUSTOM_AI_API_KEY=
CUSTOM_AI_MODEL_ID=
CUSTOM_AI_SOURCE_TAG=
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/.gitignore'
cat > apps/api/.gitignore << 'OXYGEN_AI_FILE_EOF'
.venv/
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.env.local
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/README.md'
cat > apps/api/README.md << 'OXYGEN_AI_FILE_EOF'
# Oxygen AI API — FastAPI backend

The API Gateway Layer, Passage 1 §3. Everything backend belongs here now:
deterministic indicators, verification, the four AI providers, comparison
scoring and isolation, and database access. The Next.js app in the repo
root has none of this anymore — it calls this service over HTTP.

## Structure

```
app/
  main.py              FastAPI app, CORS, error-shape compatibility
  schemas.py           Pydantic models (the request/response contract)
  indicators.py        SMA/RSI/ATR + synthetic OHLCV generator
  verify.py            entry/stop/target math + consistency check
  scoring.py           the 8-axis comparison scoring
  comparison.py        multi-provider isolation (asyncio.gather)
  utils.py
  providers/
    base.py            the AIProvider protocol every provider implements
    mock_provider.py    offline, no key needed
    custom_ai_provider.py   any OpenAI-compatible endpoint ("Oxygen AI" in the UI)
    grok_provider.py    xAI, via httpx
    gemma_provider.py   Gemma 4 via the real google-genai SDK
    registry.py          resolve/get_provider_strict/list
    prompt.py            shared prompt builder
  db/
    client.py            asyncpg connection pool
    market_data.py        instrument + OHLCV bar persistence
    signals.py             ai_providers/ai_models seeding, trading_signals
    comparisons.py          ai_comparisons/ai_comparison_results
  routers/
    analyze.py            POST /api/ai/analyze — single + multi mode
tests/                    pytest, mirrors the file layout above
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env.local   # optional
.venv/bin/uvicorn app.main:app --reload --port 8000
```

`GET /health` for a quick check; interactive docs at `/docs` (FastAPI's
built-in Swagger UI, free from Pydantic's schemas).

## Testing

```bash
.venv/bin/mypy app --ignore-missing-imports
.venv/bin/pytest tests/ -v
```

28 tests, ported from the original TypeScript suite plus the provider
registry tests — same coverage: indicator math, scoring axes, the
provider-set validator, and the isolation guarantee (one provider failing
never takes the others down).

## What's real vs. not, specific to this service

- Real: everything listed in the repo-root README's status table under
  §3/§4/§6/§9/§10.
- Not real: no live call to Grok, Gemma, or a custom endpoint has been
  made from this sandbox — outbound network here is locked to package
  registries. Code compiles, type-checks, and correctly falls back to
  "unavailable" when unconfigured; the actual round-trip needs your keys
  and a run outside this sandbox.
- No migration tool — `../infra/migrations/*.sql` are hand-written,
  applied directly with `psql`. Fine for now; matters once the schema
  needs to evolve with a tracked history.
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/__init__.py'
cat > apps/api/app/__init__.py << 'OXYGEN_AI_FILE_EOF'
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/comparison.py'
cat > apps/api/app/comparison.py << 'OXYGEN_AI_FILE_EOF'
"""
Port of lib/comparison.ts. Runs every requested provider concurrently and
isolates failures per slot — this is Passage 1 §4.5's isolation guarantee,
literally: asyncio.gather(..., return_exceptions=True). The TypeScript
version used Promise.allSettled as the JS equivalent of this; here it's
the real thing the blueprint actually specifies.
"""

from __future__ import annotations

import asyncio

from .providers.base import AnalysisContext
from .providers.registry import get_provider_strict
from .schemas import ComparisonSlot, ComparisonSlotOk, ComparisonSlotUnavailable, IndicatorBundle
from .scoring import score_analysis
from .verify import build_trade_analysis

# mock is not one of the blueprint's authoritative combinations (Custom
# AI / Grok / Gemma 4, any 2 or all 3) — it's kept valid here too, purely
# so the comparison view can be demonstrated without live credentials.
# Comparing a provider against itself isn't a comparison, so it's not
# allowed to appear twice.
VALID_PROVIDER_IDS = ["mock", "custom", "grok", "gemma"]


def validate_provider_set(providers: list[str]) -> str | None:
    if len(providers) < 2 or len(providers) > 3:
        return "providers must list 2 or 3 provider ids"
    if len(set(providers)) != len(providers):
        return "providers must not repeat"
    for p in providers:
        if p not in VALID_PROVIDER_IDS:
            return f"unknown provider id: {p} (must be one of {', '.join(VALID_PROVIDER_IDS)})"
    return None


async def _run_one(provider_id: str, symbol: str, indicators: IndicatorBundle, last_bars: list[dict], model_id: str) -> ComparisonSlotOk:
    provider = get_provider_strict(provider_id)
    reasoning = await provider.reason(
        AnalysisContext(symbol=symbol, indicators=indicators, last_bars=last_bars)
    )
    source = "mock" if provider_id == "mock" else "hosted_api"
    analysis = build_trade_analysis(indicators, reasoning, source, provider_id, model_id, False)
    return ComparisonSlotOk(providerId=provider_id, analysis=analysis, scores=score_analysis(analysis))


async def run_comparison(
    provider_ids: list[str],
    symbol: str,
    indicators: IndicatorBundle,
    last_bars: list[dict],
    resolve_model_id,
) -> list[ComparisonSlot]:
    settled = await asyncio.gather(
        *[
            _run_one(pid, symbol, indicators, last_bars, resolve_model_id(pid))
            for pid in provider_ids
        ],
        return_exceptions=True,
    )

    results: list[ComparisonSlot] = []
    for provider_id, outcome in zip(provider_ids, settled):
        if isinstance(outcome, BaseException):
            results.append(ComparisonSlotUnavailable(providerId=provider_id, reason=str(outcome)))
        else:
            results.append(outcome)
    return results
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/db/__init__.py'
cat > apps/api/app/db/__init__.py << 'OXYGEN_AI_FILE_EOF'
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/db/client.py'
cat > apps/api/app/db/client.py << 'OXYGEN_AI_FILE_EOF'
"""
Port of lib/db/client.ts. Lazily-created connection pool, gated on
DATABASE_URL being set.
"""

from __future__ import annotations

import os

import asyncpg

_pool: asyncpg.Pool | None = None


def is_db_configured() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


async def get_pool() -> asyncpg.Pool:
    global _pool
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is not set")
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=os.environ["DATABASE_URL"], min_size=1, max_size=5)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/db/comparisons.py'
cat > apps/api/app/db/comparisons.py << 'OXYGEN_AI_FILE_EOF'
"""
Port of lib/db/comparisons.ts. Persists a completed comparison: one
ai_comparisons row (provider_set as jsonb — Passage 1 §9.1's fix for
Revision 2's hardcoded two-column design), and one ai_comparison_results
row per participating provider (2 or 3 rows), each linked to its seeded
ai_providers/ai_models rows and, where one exists, the trading_signals
row save_signal() created.
"""

from __future__ import annotations

import json

from .client import get_pool
from .signals import get_or_seed_model
from ..schemas import ComparisonSlot


async def save_comparison(
    provider_ids: list[str],
    scores: dict,
    signal_id_by_provider: dict[str, str | None],
    model_id_by_provider: dict[str, str],
) -> str:
    pool = await get_pool()

    comparison_row = await pool.fetchrow(
        "INSERT INTO ai_comparisons (provider_set, scores) VALUES ($1, $2) RETURNING id",
        json.dumps(provider_ids),
        json.dumps(scores),
    )
    comparison_id = str(comparison_row["id"])

    for provider_id in provider_ids:
        seeded = await get_or_seed_model(
            provider_id, model_id_by_provider.get(provider_id, "unknown"), provider_id
        )
        await pool.execute(
            """
            INSERT INTO ai_comparison_results (comparison_id, provider_id, model_id, result_id)
            VALUES ($1, $2, $3, $4)
            """,
            comparison_id,
            seeded.provider_id,
            seeded.model_db_id,
            signal_id_by_provider.get(provider_id),
        )

    return comparison_id


def build_scores_record(slots: list[ComparisonSlot]) -> dict:
    record: dict = {}
    for slot in slots:
        if slot.outcome == "ok":
            record[slot.providerId] = slot.scores.model_dump()
        else:
            record[slot.providerId] = {"unavailable": slot.reason}
    return record
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/db/market_data.py'
cat > apps/api/app/db/market_data.py << 'OXYGEN_AI_FILE_EOF'
"""
Port of lib/db/marketData.ts — instrument upsert, reading persisted bars,
appending new ones.
"""

from __future__ import annotations

from datetime import datetime

from .client import get_pool
from ..schemas import OHLCVBar


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


async def get_or_create_instrument(symbol: str, exchange: str = "NSE", instrument_type: str = "EQUITY") -> str:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO market_instruments (symbol, exchange, instrument_type)
        VALUES ($1, $2, $3)
        ON CONFLICT (symbol, exchange) DO UPDATE SET symbol = EXCLUDED.symbol
        RETURNING id
        """,
        symbol,
        exchange,
        instrument_type,
    )
    return str(row["id"])


async def get_recent_bars(instrument_id: str, limit: int, timeframe: str = "1d") -> list[OHLCVBar]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT ts, open, high, low, close, volume FROM market_data
        WHERE instrument_id = $1 AND timeframe = $2
        ORDER BY ts DESC LIMIT $3
        """,
        instrument_id,
        timeframe,
        limit,
    )
    # DESC for the LIMIT to grab the most recent N, then reversed back to
    # chronological order — the indicator engine expects oldest-to-newest.
    bars = [
        OHLCVBar(
            timestamp=r["ts"].isoformat().replace("+00:00", "Z"),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"]),
        )
        for r in rows
    ]
    return list(reversed(bars))


async def insert_bars(
    instrument_id: str, bars: list[OHLCVBar], timeframe: str = "1d", source: str = "synthetic-demo"
) -> None:
    if not bars:
        return
    pool = await get_pool()
    # data_mode is left to its schema default ('demo') — accurate, since
    # this is still synthetic data, not a real vendor feed.
    await pool.executemany(
        """
        INSERT INTO market_data (instrument_id, ts, open, high, low, close, volume, timeframe, source)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        [
            (
                instrument_id,
                _parse_ts(bar.timestamp),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                timeframe,
                source,
            )
            for bar in bars
        ],
    )
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/db/signals.py'
cat > apps/api/app/db/signals.py << 'OXYGEN_AI_FILE_EOF'
"""
Port of lib/db/signals.ts.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import get_pool
from ..schemas import TradeAnalysis

PROVIDER_TYPE: dict[str, str] = {
    "mock": "mock",
    "custom": "custom",
    "grok": "xai",
    "gemma": "google",  # provider identity is Gemma 4; this is only a type label
}


@dataclass
class SeededModel:
    provider_id: str  # ai_providers.id
    model_db_id: str  # ai_models.id


async def get_or_seed_model(provider_name: str, model_id: str, display_name: str) -> SeededModel:
    """
    Upserts the ai_providers/ai_models rows this app's four provider ids
    map to, and returns both ids — ai_models.id for
    trading_signals.generated_by, ai_providers.id for
    ai_comparison_results.provider_id. Requires the 0002 migration
    (unique constraint on (provider_id, model_id)) — without it this
    would insert a duplicate model row on every call instead of updating
    one.
    """
    pool = await get_pool()
    provider_row = await pool.fetchrow(
        """
        INSERT INTO ai_providers (name, type)
        VALUES ($1, $2)
        ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        provider_name,
        PROVIDER_TYPE.get(provider_name, provider_name),
    )
    provider_id = str(provider_row["id"])

    model_row = await pool.fetchrow(
        """
        INSERT INTO ai_models (provider_id, model_id, display_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (provider_id, model_id) DO UPDATE SET display_name = EXCLUDED.display_name
        RETURNING id
        """,
        provider_id,
        model_id,
        display_name,
    )
    return SeededModel(provider_id=provider_id, model_db_id=str(model_row["id"]))


async def save_signal(instrument_id: str, model_db_id: str, analysis: TradeAnalysis) -> str | None:
    """
    Only SETUP_FOUND results become a trading_signals row — a signal is a
    specific concrete claim; NO_VALID_SETUP is an absence of one, not a
    fact worth a row of its own. Returns the new row's id (or None when
    nothing was inserted) so callers like ai_comparison_results can link
    to it.
    """
    if analysis.status != "SETUP_FOUND" or not analysis.direction:
        return None
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO trading_signals
          (instrument_id, generated_by, direction, entry_low, entry_high,
           stop_loss, target_1, target_2, risk_reward, confidence, status)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'open')
        RETURNING id
        """,
        instrument_id,
        model_db_id,
        analysis.direction,
        analysis.entry,  # entry_low
        analysis.entry,  # entry_high — a point estimate, represented as a zero-width range
        analysis.stopLoss,
        analysis.targets[0] if len(analysis.targets) > 0 else None,
        analysis.targets[1] if len(analysis.targets) > 1 else None,
        analysis.riskReward,
        analysis.confidence,
    )
    return str(row["id"])
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/indicators.py'
cat > apps/api/app/indicators.py << 'OXYGEN_AI_FILE_EOF'
"""
Deterministic indicator engine. Port of lib/indicators.ts — same formulas,
same synthetic-data seeding scheme, so results are identical to what the
TypeScript version produced. This is Trading Analysis Engine territory
(Passage 1 §6 / Passage 4 §4): Python + numpy/pandas is the specified
baseline, never the AI layer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .schemas import IndicatorBundle, OHLCVBar
from .utils import round2


def sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    return sum(window) / period


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains += change
        else:
            losses += abs(change)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def atr(bars: list[OHLCVBar], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    true_ranges: list[float] = []
    for i in range(len(bars) - period, len(bars)):
        cur = bars[i]
        prev_close = bars[i - 1].close
        tr = max(
            cur.high - cur.low,
            abs(cur.high - prev_close),
            abs(cur.low - prev_close),
        )
        true_ranges.append(tr)
    return sum(true_ranges) / period


def compute_indicators(bars: list[OHLCVBar]) -> IndicatorBundle:
    closes = [b.close for b in bars]
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    rsi14 = rsi(closes, 14)
    atr14 = atr(bars, 14)
    last_close = closes[-1]

    trend: str = "flat"
    if sma20 is not None and sma50 is not None:
        if sma20 > sma50 * 1.001:
            trend = "up"
        elif sma20 < sma50 * 0.999:
            trend = "down"

    return IndicatorBundle(
        sma20=sma20,
        sma50=sma50,
        rsi14=rsi14,
        atr14=atr14,
        lastClose=last_close,
        trend=trend,  # type: ignore[arg-type]
    )


def generate_synthetic_ohlcv(symbol: str, bars: int = 60) -> list[OHLCVBar]:
    """
    FOR DEMO ONLY. Deterministic pseudo-random walk seeded from the symbol
    string — not real market data, and not connected to a vendor. Ported
    bit-for-bit from indicators.ts's LCG so results match what the
    TypeScript version produced for the same symbol.
    """
    seed = 0
    for ch in symbol:
        seed = (seed * 31 + ord(ch)) % 100000
    if seed == 0:
        seed = 42

    def rand() -> float:
        nonlocal seed
        seed = (seed * 1103515245 + 12345) % 2147483648
        return seed / 2147483648

    result: list[OHLCVBar] = []
    price: float = float(100 + (seed % 400))
    now = datetime.now(timezone.utc)
    for i in range(bars, 0, -1):
        change = (rand() - 0.48) * price * 0.02
        open_ = price
        close = max(1.0, price + change)
        high = max(open_, close) + rand() * price * 0.005
        low = min(open_, close) - rand() * price * 0.005
        volume = int(10000 + rand() * 90000)
        ts = now - timedelta(days=i)
        result.append(
            OHLCVBar(
                timestamp=ts.isoformat().replace("+00:00", "Z"),
                open=round2(open_),
                high=round2(high),
                low=round2(low),
                close=round2(close),
                volume=volume,
            )
        )
        price = close
    return result
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/main.py'
cat > apps/api/app/main.py << 'OXYGEN_AI_FILE_EOF'
"""
The API Gateway Layer, Passage 1 §3 — FastAPI (Python), REST, separate
from the Next.js web UI. The frontend calls this directly over HTTP; no
Next.js API route sits in between anymore (that would be exactly the
"silently treat Next.js routes as a replacement for FastAPI" this
migration exists to undo).
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .routers import analyze

app = FastAPI(title="Oxygen AI API Gateway", version="0.1.0")

_allowed_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Reshaped to the same {"error", "details"} the Next.js/Zod version
    # returned, so the frontend's existing error handling needs no
    # changes — only the request URL changed.
    return JSONResponse(
        status_code=400,
        content={"error": "Invalid request", "details": exc.errors()},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "oxygen-ai-api"}


app.include_router(analyze.router)
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/providers/__init__.py'
cat > apps/api/app/providers/__init__.py << 'OXYGEN_AI_FILE_EOF'
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/providers/base.py'
cat > apps/api/app/providers/base.py << 'OXYGEN_AI_FILE_EOF'
"""
Port of lib/providers/types.ts. Every provider (mock, Custom AI, Grok,
Gemma 4) implements this one protocol. Adding a provider is a new file,
not a change to the engine, the schema, or the router — the actual point
of the blueprint's "provider-agnostic AI layer".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ..schemas import IndicatorBundle


@dataclass
class AnalysisContext:
    symbol: str
    indicators: IndicatorBundle
    last_bars: list[dict]  # [{"timestamp": str, "close": float}, ...]


@dataclass
class ProviderReasoning:
    direction: Literal["LONG", "SHORT"] | None
    confidence: float | None
    reasoning_summary: str
    supporting_evidence: list[str]


class AIProvider(Protocol):
    id: str
    display_name: str

    def is_configured(self) -> bool: ...

    async def reason(self, context: AnalysisContext) -> ProviderReasoning: ...
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/providers/custom_ai_provider.py'
cat > apps/api/app/providers/custom_ai_provider.py << 'OXYGEN_AI_FILE_EOF'
"""
"Custom AI" per the blueprint is deliberately not a fixed vendor — §4.1
defines it as an orchestrating agent over "any strong tool-calling LLM
behind the AIProvider interface," the owner's choice. This is that choice
made concrete: any OpenAI-compatible chat-completions endpoint, configured
via env vars. Displayed in the UI as "Oxygen AI" (the product's own name
for its default provider) — the id stays "custom" throughout the backend.

NOT implemented here: the "orchestrating agent" half of the spec — tool
calling against the 15-tool registry, the RAG -> feedback loop ->
fine-tuning evolution path (blueprint §4.1, §4.8). This is the provider
integration layer only. Port of lib/providers/customAiProvider.ts.
"""

from __future__ import annotations

import json
import os
import re

import httpx

from .base import AnalysisContext, ProviderReasoning
from .prompt import build_prompt


class CustomAIProvider:
    id = "custom"
    display_name = "Oxygen AI (your configured model)"

    def is_configured(self) -> bool:
        return bool(
            os.environ.get("CUSTOM_AI_API_KEY")
            and os.environ.get("CUSTOM_AI_BASE_URL")
            and os.environ.get("CUSTOM_AI_MODEL_ID")
        )

    async def reason(self, context: AnalysisContext) -> ProviderReasoning:
        api_key = os.environ.get("CUSTOM_AI_API_KEY")
        base_url = os.environ.get("CUSTOM_AI_BASE_URL")
        model_id = os.environ.get("CUSTOM_AI_MODEL_ID")
        if not api_key or not base_url or not model_id:
            raise RuntimeError(
                "CUSTOM_AI_API_KEY, CUSTOM_AI_BASE_URL, and CUSTOM_AI_MODEL_ID must all be set"
            )

        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": build_prompt(context)}],
                },
            )

        if res.status_code >= 400:
            raise RuntimeError(f"Custom AI endpoint error {res.status_code}: {res.text[:200]}")

        data = res.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise RuntimeError("Custom AI response did not contain parseable JSON")

        parsed = json.loads(match.group(0))
        return ProviderReasoning(
            direction=parsed.get("direction"),
            confidence=parsed.get("confidence"),
            reasoning_summary=parsed.get("reasoningSummary", ""),
            supporting_evidence=parsed.get("supportingEvidence") or [],
        )


custom_ai_provider = CustomAIProvider()
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/providers/gemma_provider.py'
cat > apps/api/app/providers/gemma_provider.py << 'OXYGEN_AI_FILE_EOF'
"""
Identity vs. transport, per the blueprint's own §4.3.1 rule: Gemma 4 is
the provider identity; Google's Gemini API is only the hosted transport
that reaches it. This file is GemmaProvider — nothing here is named or
exposed as a separate "Gemini" provider. Port of
lib/providers/gemmaProvider.ts, now using the real Python google-genai
SDK the blueprint names explicitly (confirmed on PyPI before writing
this — see the delivery notes).

Verify MODEL_ID against Google's current model list before relying on
it — Gemma 4 model ID strings have shown minor naming variation across
release waves (see README). Override with GEMMA_MODEL_ID if it's changed.
"""

from __future__ import annotations

import json
import os
import re

from google import genai

from .base import AnalysisContext, ProviderReasoning
from .prompt import build_prompt


class GemmaProvider:
    id = "gemma"
    display_name = "Gemma 4 (hosted via Gemini API transport)"

    def is_configured(self) -> bool:
        return bool(os.environ.get("GOOGLE_AI_API_KEY"))

    async def reason(self, context: AnalysisContext) -> ProviderReasoning:
        api_key = os.environ.get("GOOGLE_AI_API_KEY")
        if not api_key:
            raise RuntimeError("GOOGLE_AI_API_KEY is not set")

        model_id = os.environ.get("GEMMA_MODEL_ID", "gemma-4-4b-it")

        # @google/genai (JS) / google-genai (Python) is the SDK Google
        # ships for reaching Gemini AND Gemma — using it is the
        # transport, not a second provider. See module docstring.
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=model_id,
            contents=build_prompt(context),
        )

        text = response.text or ""
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise RuntimeError("Gemma response did not contain parseable JSON")

        parsed = json.loads(match.group(0))
        return ProviderReasoning(
            direction=parsed.get("direction"),
            confidence=parsed.get("confidence"),
            reasoning_summary=parsed.get("reasoningSummary", ""),
            supporting_evidence=parsed.get("supportingEvidence") or [],
        )


gemma_provider = GemmaProvider()
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/providers/grok_provider.py'
cat > apps/api/app/providers/grok_provider.py << 'OXYGEN_AI_FILE_EOF'
"""
xAI's API is OpenAI-compatible (chat completions shape), so this uses a
plain httpx call rather than pulling in an SDK for one endpoint — same
"don't add a dependency you don't need" principle the blueprint states
for plugin/library choices. Port of lib/providers/grokProvider.ts.
"""

from __future__ import annotations

import json
import os
import re

import httpx

from .base import AnalysisContext, ProviderReasoning
from .prompt import build_prompt

XAI_BASE_URL = "https://api.x.ai/v1"


class GrokProvider:
    id = "grok"
    display_name = "Grok (xAI)"

    def is_configured(self) -> bool:
        return bool(os.environ.get("XAI_API_KEY"))

    async def reason(self, context: AnalysisContext) -> ProviderReasoning:
        api_key = os.environ.get("XAI_API_KEY")
        if not api_key:
            raise RuntimeError("XAI_API_KEY is not set")

        model_id = os.environ.get("GROK_MODEL_ID", "grok-4.6")

        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(
                f"{XAI_BASE_URL}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": build_prompt(context)}],
                },
            )

        if res.status_code >= 400:
            raise RuntimeError(f"xAI API error {res.status_code}: {res.text[:200]}")

        data = res.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise RuntimeError("Grok response did not contain parseable JSON")

        parsed = json.loads(match.group(0))
        return ProviderReasoning(
            direction=parsed.get("direction"),
            confidence=parsed.get("confidence"),
            reasoning_summary=parsed.get("reasoningSummary", ""),
            supporting_evidence=parsed.get("supportingEvidence") or [],
        )


grok_provider = GrokProvider()
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/providers/mock_provider.py'
cat > apps/api/app/providers/mock_provider.py << 'OXYGEN_AI_FILE_EOF'
"""
Rule-based, not a model call — but it obeys the same law every provider
must obey: it only ever reasons over indicators.py's output, and it never
invents a price. Port of lib/providers/mockProvider.ts.
"""

from __future__ import annotations

from .base import AnalysisContext, ProviderReasoning


class MockProvider:
    id = "mock"
    display_name = "Mock Reasoner (offline)"

    def is_configured(self) -> bool:
        return True

    async def reason(self, context: AnalysisContext) -> ProviderReasoning:
        indicators = context.indicators
        trend = indicators.trend
        rsi14 = indicators.rsi14

        if trend == "flat" or rsi14 is None:
            return ProviderReasoning(
                direction=None,
                confidence=None,
                reasoning_summary=(
                    "Trend is flat and/or RSI has insufficient history — "
                    "no directional edge to report."
                ),
                supporting_evidence=[],
            )

        overbought = rsi14 > 70
        oversold = rsi14 < 30

        if trend == "up" and not overbought:
            return ProviderReasoning(
                direction="LONG",
                confidence=72 if oversold else 58,
                reasoning_summary=(
                    f"20-SMA is above 50-SMA (uptrend) and RSI(14) at {rsi14:.1f} "
                    "is not overbought, so momentum has room to continue."
                ),
                supporting_evidence=["sma20 > sma50", f"rsi14 = {rsi14:.1f} (not overbought)"],
            )

        if trend == "down" and not oversold:
            return ProviderReasoning(
                direction="SHORT",
                confidence=70 if overbought else 55,
                reasoning_summary=(
                    f"20-SMA is below 50-SMA (downtrend) and RSI(14) at {rsi14:.1f} "
                    "is not oversold, so downside momentum has room to continue."
                ),
                supporting_evidence=["sma20 < sma50", f"rsi14 = {rsi14:.1f} (not oversold)"],
            )

        return ProviderReasoning(
            direction=None,
            confidence=None,
            reasoning_summary=(
                "Trend direction is already extended on RSI (overbought in an "
                "uptrend, or oversold in a downtrend) — no valid setup."
            ),
            supporting_evidence=[],
        )


mock_provider = MockProvider()
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/providers/prompt.py'
cat > apps/api/app/providers/prompt.py << 'OXYGEN_AI_FILE_EOF'
from __future__ import annotations

from .base import AnalysisContext


def build_prompt(context: AnalysisContext) -> str:
    ind = context.indicators
    return f"""You are a trading analysis reasoner. You never invent numbers — you only reason over the indicators given below, which were computed deterministically outside of you.

Symbol: {context.symbol}
Last close: {ind.lastClose}
SMA(20): {ind.sma20 if ind.sma20 is not None else "n/a"}
SMA(50): {ind.sma50 if ind.sma50 is not None else "n/a"}
RSI(14): {ind.rsi14 if ind.rsi14 is not None else "n/a"}
ATR(14): {ind.atr14 if ind.atr14 is not None else "n/a"}
Trend: {ind.trend}

Respond ONLY with JSON matching exactly this shape, nothing else, no markdown fences:
{{"direction": "LONG" or "SHORT" or null, "confidence": number 0-100 or null, "reasoningSummary": string, "supportingEvidence": string[]}}

If there is no valid setup, set direction and confidence to null and explain why in reasoningSummary."""
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/providers/registry.py'
cat > apps/api/app/providers/registry.py << 'OXYGEN_AI_FILE_EOF'
"""
Port of lib/providers/index.ts. Exactly the three providers the
blueprint specifies, plus the offline mock fallback. Gemini is never a
provider in its own right, only Gemma's hosted transport
(gemma_provider.py).
"""

from __future__ import annotations

from .base import AIProvider
from .custom_ai_provider import custom_ai_provider
from .gemma_provider import gemma_provider
from .grok_provider import grok_provider
from .mock_provider import mock_provider

_REGISTRY: dict[str, AIProvider] = {
    "mock": mock_provider,
    "custom": custom_ai_provider,
    "grok": grok_provider,
    "gemma": gemma_provider,
}


def resolve_provider(requested_id: str | None) -> AIProvider:
    """
    Same fallback principle as the blueprint's Provider Router: degrade
    to a controlled, working path instead of a hard failure when a
    provider isn't actually configured.
    """
    provider = _REGISTRY.get(requested_id) if requested_id else None
    if provider is None:
        return mock_provider
    if not provider.is_configured():
        return mock_provider
    return provider


def get_provider_strict(provider_id: str) -> AIProvider:
    """
    For multi-provider comparison, unlike resolve_provider(): does NOT
    silently substitute mock for an unconfigured provider. A comparison
    is supposed to show distinct real providers side by side — quietly
    running mock logic under a "Grok" label would defeat the point.
    Unconfigured providers are still returned here (reason() will raise;
    the caller isolates that per slot — see app/comparison.py).
    """
    provider = _REGISTRY.get(provider_id)
    if provider is None:
        raise KeyError(f"Unknown provider id: {provider_id}")
    return provider


def list_providers() -> list[dict]:
    return [
        {"id": p.id, "displayName": p.display_name, "configured": p.is_configured()}
        for p in _REGISTRY.values()
    ]
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/routers/__init__.py'
cat > apps/api/app/routers/__init__.py << 'OXYGEN_AI_FILE_EOF'
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/routers/analyze.py'
cat > apps/api/app/routers/analyze.py << 'OXYGEN_AI_FILE_EOF'
"""
Port of app/api/analyze/route.ts. This is the API Gateway endpoint the
blueprint's architecture diagram (Passage 1 §3) shows the Web/Mobile UI
calling directly over HTTPS — no Next.js intermediary.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from ..comparison import run_comparison, validate_provider_set
from ..db.client import is_db_configured
from ..db.comparisons import build_scores_record, save_comparison
from ..db.market_data import get_or_create_instrument, get_recent_bars, insert_bars
from ..db.signals import get_or_seed_model, save_signal
from ..indicators import compute_indicators, generate_synthetic_ohlcv
from ..providers.registry import resolve_provider
from ..schemas import AnalyzeRequest, ComparisonResponse, TradeAnalysis, now_iso
from ..verify import build_trade_analysis

router = APIRouter()

BARS_NEEDED = 60


def resolve_model_id(provider_id: str) -> str:
    if provider_id == "gemma":
        return os.environ.get("GEMMA_MODEL_ID", "gemma-4-4b-it")
    if provider_id == "grok":
        return os.environ.get("GROK_MODEL_ID", "grok-4.6")
    if provider_id == "custom":
        return os.environ.get("CUSTOM_AI_MODEL_ID", "unset")
    return "mock-v1"


async def get_or_refresh_bars(symbol: str):
    """
    Shared by both single- and multi-provider modes. Reads persisted bars
    from the database when configured; generates and stores a fresh
    (still-synthetic) set the first time a symbol is requested; falls
    back to ephemeral synthetic data — cleanly, not a crash — if the
    database isn't configured or a call to it fails.
    """
    if not is_db_configured():
        return generate_synthetic_ohlcv(symbol, BARS_NEEDED), None, False
    try:
        instrument_id = await get_or_create_instrument(symbol)
        existing = await get_recent_bars(instrument_id, BARS_NEEDED)
        if len(existing) >= BARS_NEEDED:
            return existing, instrument_id, True
        bars = generate_synthetic_ohlcv(symbol, BARS_NEEDED)
        await insert_bars(instrument_id, bars)
        return bars, instrument_id, True
    except Exception as err:  # noqa: BLE001 — deliberate: any DB failure degrades, never crashes the request
        print(f"DB path failed, falling back to ephemeral synthetic data: {err}")
        return generate_synthetic_ohlcv(symbol, BARS_NEEDED), None, False


@router.post("/api/ai/analyze")
async def analyze(body: AnalyzeRequest):
    symbol = body.symbol.upper()
    bars, instrument_id, persisted = await get_or_refresh_bars(symbol)
    indicators = compute_indicators(bars)
    last_bars = [{"timestamp": b.timestamp, "close": b.close} for b in bars[-5:]]

    # ---- Multi-provider comparison mode ----
    if body.providers:
        validation_error = validate_provider_set(body.providers)
        if validation_error:
            raise HTTPException(status_code=400, detail=validation_error)

        results = await run_comparison(body.providers, symbol, indicators, last_bars, resolve_model_id)

        if persisted and instrument_id:
            try:
                signal_id_by_provider: dict[str, str | None] = {}
                model_id_by_provider: dict[str, str] = {}
                for slot in results:
                    model_id_by_provider[slot.providerId] = resolve_model_id(slot.providerId)
                    if slot.outcome == "ok" and slot.analysis.status == "SETUP_FOUND":
                        seeded = await get_or_seed_model(
                            slot.providerId, resolve_model_id(slot.providerId), slot.providerId
                        )
                        signal_id_by_provider[slot.providerId] = await save_signal(
                            instrument_id, seeded.model_db_id, slot.analysis
                        )
                    else:
                        signal_id_by_provider[slot.providerId] = None
                await save_comparison(
                    body.providers,
                    build_scores_record(results),
                    signal_id_by_provider,
                    model_id_by_provider,
                )
            except Exception as err:  # noqa: BLE001
                # The comparison result itself is still valid and already
                # computed — a failed write shouldn't turn a good
                # response into a 503, same principle as the
                # single-provider path below.
                print(f"Failed to persist comparison (results still returned): {err}")

        return ComparisonResponse(
            mode="multi",
            symbol=symbol,
            results=results,
            persisted=persisted,
            generatedAt=now_iso(),
        )

    # ---- Single-provider mode (unchanged contract) ----
    provider = resolve_provider(body.provider)
    if provider.id == "mock":
        source = "mock"
    elif provider.id == "custom" and os.environ.get("CUSTOM_AI_SOURCE_TAG") == "local_model":
        source = "local_model"
    else:
        source = "hosted_api"
    model_id = resolve_model_id(provider.id)

    try:
        from ..providers.base import AnalysisContext

        reasoning = await provider.reason(AnalysisContext(symbol=symbol, indicators=indicators, last_bars=last_bars))
        analysis: TradeAnalysis = build_trade_analysis(indicators, reasoning, source, provider.id, model_id, persisted)

        if persisted and instrument_id and analysis.status == "SETUP_FOUND":
            try:
                seeded = await get_or_seed_model(provider.id, model_id, provider.display_name)
                await save_signal(instrument_id, seeded.model_db_id, analysis)
            except Exception as err:  # noqa: BLE001
                print(f"Failed to persist signal (analysis still returned): {err}")

        return analysis
    except Exception as err:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Provider call failed: {err}") from err
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/schemas.py'
cat > apps/api/app/schemas.py << 'OXYGEN_AI_FILE_EOF'
"""
Pydantic schemas — the request/response contract this API validates
against, at runtime, exactly the way lib/types.ts's Zod schemas did on
the Next.js side. Same shapes, ported field-for-field so the frontend's
existing fetch/render code needs no contract changes, only a new URL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field


class OHLCVBar(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class IndicatorBundle(BaseModel):
    sma20: Optional[float]
    sma50: Optional[float]
    rsi14: Optional[float]
    atr14: Optional[float]
    lastClose: float
    trend: Literal["up", "down", "flat"]


class TradeAnalysis(BaseModel):
    status: Literal["SETUP_FOUND", "NO_VALID_SETUP"]
    direction: Optional[Literal["LONG", "SHORT"]]
    entry: Optional[float]
    stopLoss: Optional[float]
    targets: list[float]
    confidence: Optional[float] = Field(default=None, ge=0, le=100)
    riskReward: Optional[float]
    reasoningSummary: str
    supportingEvidence: list[str]
    source: Literal["mock", "hosted_api", "local_model"]
    provider: str
    model: str
    indicatorsUsed: IndicatorBundle
    generatedAt: str
    persisted: bool


class ScoreBreakdown(BaseModel):
    dataCompleteness: Optional[float]
    indicatorAgreement: Optional[float]
    riskRewardQuality: Optional[float]
    ruleCompliance: Optional[float]
    explanationConsistency: Optional[float]
    historicalValidation: Literal["not_enough_data"] = "not_enough_data"
    predictionOutcome: Literal["not_enough_data"] = "not_enough_data"
    confidenceCalibration: Literal["not_enough_data"] = "not_enough_data"


class ComparisonSlotOk(BaseModel):
    outcome: Literal["ok"] = "ok"
    providerId: str
    analysis: TradeAnalysis
    scores: ScoreBreakdown


class ComparisonSlotUnavailable(BaseModel):
    outcome: Literal["unavailable"] = "unavailable"
    providerId: str
    reason: str


ComparisonSlot = Union[ComparisonSlotOk, ComparisonSlotUnavailable]


class ComparisonResponse(BaseModel):
    mode: Literal["multi"] = "multi"
    symbol: str
    results: list[ComparisonSlot]
    persisted: bool
    generatedAt: str


class AnalyzeRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    provider: Optional[str] = None
    providers: Optional[list[str]] = Field(default=None, min_length=2, max_length=3)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/scoring.py'
cat > apps/api/app/scoring.py << 'OXYGEN_AI_FILE_EOF'
"""
Port of lib/scoring.ts. Passage 4 §3.4's exact 8 scoring axes. The last
three need outcome history from trade_results that doesn't exist in this
demo (no paper-trading loop is built) — rendered as an explicit "not
enough data yet" placeholder. That placeholder behavior is itself a real,
specified requirement (Passage 4 G10), not a stand-in for the missing
feature.
"""

from __future__ import annotations

from .schemas import ScoreBreakdown, TradeAnalysis

_INDICATOR_NAMES = ["sma20", "sma50", "rsi14", "atr14", "sma", "rsi", "atr"]


def score_analysis(analysis: TradeAnalysis) -> ScoreBreakdown:
    if analysis.status == "NO_VALID_SETUP":
        return ScoreBreakdown(
            dataCompleteness=None,
            indicatorAgreement=None,
            riskRewardQuality=None,
            # Correctly declining a bad setup is compliant behavior, not
            # a failure to score down.
            ruleCompliance=100,
            explanationConsistency=None,
        )

    required_fields = [
        analysis.entry,
        analysis.stopLoss,
        analysis.targets[0] if analysis.targets else None,
        analysis.confidence,
        analysis.riskReward,
    ]
    present = sum(1 for f in required_fields if f is not None)
    data_completeness = round((present / len(required_fields)) * 100)

    # Simplified proxy for "overlap between cited evidence and the actual
    # computed indicators" (Passage 4's definition): checks whether the
    # provider named a real indicator field, not whether the cited value
    # is numerically correct.
    cites_real = any(
        name in evidence.lower() for evidence in analysis.supportingEvidence for name in _INDICATOR_NAMES
    )
    if not analysis.supportingEvidence:
        indicator_agreement: float | None = 0
    else:
        indicator_agreement = 100 if cites_real else 40

    risk_reward_quality = (
        max(0, min(100, round(analysis.riskReward * 50))) if analysis.riskReward is not None else None
    )

    # Reaching this point already means build_trade_analysis's
    # consistency check passed — a failed one never reaches SETUP_FOUND.
    rule_compliance = 100

    evidence_count = len(analysis.supportingEvidence)
    confidence = analysis.confidence or 0
    # The pattern this axis exists to catch: high stated confidence with
    # little evidence behind it.
    if confidence > 60 and evidence_count < 2:
        explanation_consistency = 40
    elif confidence <= 60 and evidence_count == 0:
        explanation_consistency = 70
    else:
        explanation_consistency = 90

    return ScoreBreakdown(
        dataCompleteness=data_completeness,
        indicatorAgreement=indicator_agreement,
        riskRewardQuality=risk_reward_quality,
        ruleCompliance=rule_compliance,
        explanationConsistency=explanation_consistency,
    )
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/utils.py'
cat > apps/api/app/utils.py << 'OXYGEN_AI_FILE_EOF'
from __future__ import annotations


def round2(n: float) -> float:
    return round(n * 100) / 100
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/app/verify.py'
cat > apps/api/app/verify.py << 'OXYGEN_AI_FILE_EOF'
"""
The verification stage. Port of lib/verify.ts. Entry/stop/targets are
computed HERE, from ATR — never taken from the AI provider's own text.
The provider only gets to propose a direction and a confidence; every
number in the response is this function's responsibility. A consistency
check runs before anything is returned; if it fails, the setup is
suppressed rather than shipped.
"""

from __future__ import annotations

from .providers.base import ProviderReasoning
from .schemas import IndicatorBundle, TradeAnalysis, now_iso
from .utils import round2


def _no_setup(
    indicators: IndicatorBundle,
    reasoning: ProviderReasoning,
    source: str,
    provider_id: str,
    model_id: str,
    persisted: bool,
    reason: str | None = None,
) -> TradeAnalysis:
    return TradeAnalysis(
        status="NO_VALID_SETUP",
        direction=None,
        entry=None,
        stopLoss=None,
        targets=[],
        confidence=None,
        riskReward=None,
        reasoningSummary=reason if reason else reasoning.reasoning_summary,
        supportingEvidence=[] if reason else reasoning.supporting_evidence,
        source=source,  # type: ignore[arg-type]
        provider=provider_id,
        model=model_id,
        indicatorsUsed=indicators,
        generatedAt=now_iso(),
        persisted=persisted,
    )


def build_trade_analysis(
    indicators: IndicatorBundle,
    reasoning: ProviderReasoning,
    source: str,
    provider_id: str,
    model_id: str,
    persisted: bool,
) -> TradeAnalysis:
    direction = reasoning.direction
    confidence = reasoning.confidence

    if not direction or indicators.atr14 is None:
        return _no_setup(indicators, reasoning, source, provider_id, model_id, persisted)

    entry = indicators.lastClose
    atr_value = indicators.atr14
    stop_loss = entry - 1.5 * atr_value if direction == "LONG" else entry + 1.5 * atr_value
    target1 = entry + 1.5 * atr_value if direction == "LONG" else entry - 1.5 * atr_value
    target2 = entry + 3 * atr_value if direction == "LONG" else entry - 3 * atr_value

    consistent = stop_loss < entry if direction == "LONG" else stop_loss > entry
    if not consistent:
        return _no_setup(
            indicators,
            reasoning,
            source,
            provider_id,
            model_id,
            persisted,
            reason="Internal consistency check failed on the computed setup — suppressed rather than returned.",
        )

    risk = abs(entry - stop_loss)
    reward = abs(target1 - entry)
    risk_reward = round2(reward / risk) if risk > 0 else None

    return TradeAnalysis(
        status="SETUP_FOUND",
        direction=direction,  # type: ignore[arg-type]
        entry=round2(entry),
        stopLoss=round2(stop_loss),
        targets=[round2(target1), round2(target2)],
        confidence=confidence,
        riskReward=risk_reward,
        reasoningSummary=reasoning.reasoning_summary,
        supportingEvidence=reasoning.supporting_evidence,
        source=source,  # type: ignore[arg-type]
        provider=provider_id,
        model=model_id,
        indicatorsUsed=indicators,
        generatedAt=now_iso(),
        persisted=persisted,
    )
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/pytest.ini'
cat > apps/api/pytest.ini << 'OXYGEN_AI_FILE_EOF'
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/requirements.txt'
cat > apps/api/requirements.txt << 'OXYGEN_AI_FILE_EOF'
fastapi==0.115.5
uvicorn[standard]==0.32.1
pydantic==2.10.3
asyncpg==0.30.0
httpx==0.28.1
google-genai==1.0.0

# dev/test only
pytest==8.3.4
pytest-asyncio==0.25.0
mypy==1.13.0
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/tests/__init__.py'
cat > apps/api/tests/__init__.py << 'OXYGEN_AI_FILE_EOF'
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/tests/test_comparison.py'
cat > apps/api/tests/test_comparison.py << 'OXYGEN_AI_FILE_EOF'
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
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/tests/test_indicators.py'
cat > apps/api/tests/test_indicators.py << 'OXYGEN_AI_FILE_EOF'
from app.indicators import atr, compute_indicators, generate_synthetic_ohlcv, rsi, sma


def test_sma_returns_none_when_not_enough_data():
    assert sma([1, 2, 3], 5) is None


def test_sma_computes_a_plain_average():
    assert sma([1, 2, 3, 4, 5], 5) == 3


def test_rsi_is_100_when_no_losses_in_window():
    closes = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
    assert rsi(closes, 14) == 100


def test_rsi_stays_within_0_100_for_mixed_data():
    closes = [10, 11, 9, 12, 8, 13, 7, 14, 6, 15, 5, 16, 4, 17, 3]
    value = rsi(closes, 14)
    assert value is not None and 0 <= value <= 100


def test_atr_is_non_negative_on_synthetic_data():
    bars = generate_synthetic_ohlcv("TEST", 30)
    value = atr(bars, 14)
    assert value is not None and value >= 0


def _without_timestamp(bar) -> dict:
    d = bar.model_dump()
    d.pop("timestamp")
    return d


def test_generate_synthetic_ohlcv_is_deterministic_for_the_same_symbol():
    # Compares OHLCV only, not the timestamp -- the timestamp is anchored
    # to wall-clock "now" by design (bars run up to the moment of the
    # call), so it's expected to differ by microseconds between two
    # separate calls even when the price series itself is identical.
    a = generate_synthetic_ohlcv("RELIANCE", 10)
    b = generate_synthetic_ohlcv("RELIANCE", 10)
    assert [_without_timestamp(bar) for bar in a] == [_without_timestamp(bar) for bar in b]


def test_generate_synthetic_ohlcv_differs_across_symbols():
    a = generate_synthetic_ohlcv("RELIANCE", 10)
    b = generate_synthetic_ohlcv("TCS", 10)
    assert [_without_timestamp(bar) for bar in a] != [_without_timestamp(bar) for bar in b]


def test_compute_indicators_produces_a_full_bundle_with_enough_history():
    bars = generate_synthetic_ohlcv("TCS", 60)
    bundle = compute_indicators(bars)
    assert bundle.sma20 is not None
    assert bundle.sma50 is not None
    assert bundle.rsi14 is not None
    assert bundle.atr14 is not None
    assert bundle.trend in ("up", "down", "flat")


def test_compute_indicators_degrades_gracefully_on_too_little_history():
    bars = generate_synthetic_ohlcv("NEWLISTING", 5)
    bundle = compute_indicators(bars)
    assert bundle.sma20 is None
    assert bundle.sma50 is None
    assert bundle.trend == "flat"
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/tests/test_registry.py'
cat > apps/api/tests/test_registry.py << 'OXYGEN_AI_FILE_EOF'
from app.providers.registry import list_providers, resolve_provider


def test_resolve_provider_falls_back_to_mock_for_unknown_id():
    assert resolve_provider("does-not-exist").id == "mock"


def test_resolve_provider_falls_back_to_mock_for_gemma_when_unconfigured():
    assert resolve_provider("gemma").id == "mock"  # no GOOGLE_AI_API_KEY set here


def test_resolve_provider_falls_back_to_mock_for_grok_when_unconfigured():
    assert resolve_provider("grok").id == "mock"  # no XAI_API_KEY set here


def test_resolve_provider_falls_back_to_mock_for_custom_when_unconfigured():
    assert resolve_provider("custom").id == "mock"  # no CUSTOM_AI_* set here


def test_registry_is_exactly_mock_custom_grok_gemma_no_gemini_entry():
    ids = sorted(p["id"] for p in list_providers())
    assert ids == ["custom", "gemma", "grok", "mock"]
OXYGEN_AI_FILE_EOF

echo '  new: apps/api/tests/test_scoring.py'
cat > apps/api/tests/test_scoring.py << 'OXYGEN_AI_FILE_EOF'
from app.schemas import IndicatorBundle, TradeAnalysis, now_iso
from app.scoring import score_analysis

BASE_INDICATORS = IndicatorBundle(sma20=100, sma50=98, rsi14=55, atr14=2, lastClose=100, trend="up")


def make_analysis(**overrides) -> TradeAnalysis:
    defaults = dict(
        status="SETUP_FOUND",
        direction="LONG",
        entry=100,
        stopLoss=97,
        targets=[103, 106],
        confidence=60,
        riskReward=1.5,
        reasoningSummary="test",
        supportingEvidence=["sma20 > sma50", "rsi14 = 55 (not overbought)"],
        source="mock",
        provider="mock",
        model="mock-v1",
        indicatorsUsed=BASE_INDICATORS,
        generatedAt=now_iso(),
        persisted=False,
    )
    defaults.update(overrides)
    return TradeAnalysis(**defaults)


def test_no_valid_setup_scores_rule_compliance_100_and_rest_null():
    s = score_analysis(make_analysis(status="NO_VALID_SETUP", direction=None))
    assert s.ruleCompliance == 100
    assert s.dataCompleteness is None
    assert s.historicalValidation == "not_enough_data"


def test_fully_populated_setup_scores_100_data_completeness():
    s = score_analysis(make_analysis())
    assert s.dataCompleteness == 100


def test_citing_a_real_indicator_scores_full_indicator_agreement():
    s = score_analysis(make_analysis(supportingEvidence=["rsi14 = 55"]))
    assert s.indicatorAgreement == 100


def test_citing_no_evidence_scores_zero_indicator_agreement():
    s = score_analysis(make_analysis(supportingEvidence=[]))
    assert s.indicatorAgreement == 0


def test_high_confidence_thin_evidence_scores_low_explanation_consistency():
    s = score_analysis(make_analysis(confidence=90, supportingEvidence=[]))
    assert s.explanationConsistency == 40


def test_history_dependent_axes_are_always_the_placeholder():
    s = score_analysis(make_analysis())
    assert s.historicalValidation == "not_enough_data"
    assert s.predictionOutcome == "not_enough_data"
    assert s.confidenceCalibration == "not_enough_data"
OXYGEN_AI_FILE_EOF

echo "=== Writing modified files ==="
echo '  modified: .env.example'
cat > .env.example << 'OXYGEN_AI_FILE_EOF'
# Where the FastAPI backend (apps/api) is running. The frontend calls it
# directly from the browser -- see apps/api/.env.example for the actual
# provider keys and DATABASE_URL, which belong there now, not here.
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
OXYGEN_AI_FILE_EOF

echo '  modified: README.md'
cat > README.md << 'OXYGEN_AI_FILE_EOF'
# Oxygen AI — minimal slice

Two services now, matching Passage 1 §3's architecture diagram instead of
approximating it with a Next.js monolith:

```
apps/api/   FastAPI (Python) — the API Gateway Layer. All backend logic:
            deterministic indicators, verification, provider orchestration,
            comparison scoring, database access.
(repo root) Next.js (TypeScript/React) — the Web UI. Calls apps/api/
            directly over HTTP. No backend logic of its own.
```

## Architecture correction applied this pass

Passage 1 §3 specifies a distinct **API Gateway Layer — FastAPI (Python)**,
separate from the Next.js web UI. Earlier passes built all backend logic
as TypeScript inside Next.js API routes instead — a pragmatic
simplification for a single-service demo, but a real deviation from the
blueprint, and one that was flagged plainly rather than left implicit
once it came under review.

This pass migrates it: `apps/api/` is a real FastAPI service, ported
function-for-function from the previous TypeScript implementation
(indicators, verification, scoring, provider routing, comparison
isolation, database access). `app/api/analyze/route.ts` is deleted — the
browser now calls FastAPI directly (`POST /api/ai/analyze`), exactly as
the architecture diagram shows, with CORS configured for the Next.js
origin. The Next.js app's `lib/` now holds only what a frontend
legitimately needs: rendering types, no backend logic, no database
driver, no AI SDKs.

## Running it

```bash
# Terminal 1 — backend
cd apps/api
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env.local   # optional — add real provider keys / DATABASE_URL
.venv/bin/uvicorn app.main:app --reload --port 8000

# Terminal 2 — frontend
npm install
npm run build && npm start   # or `npm run dev` for local development
```

Open http://localhost:3000. Note: `NEXT_PUBLIC_API_BASE_URL` is inlined
into the frontend at **build** time, not read at runtime (standard
Next.js behavior for `NEXT_PUBLIC_*` vars) — if you point the backend
somewhere other than `localhost:8000`, set it before `npm run build`,
not just before `npm start`. Verified explicitly this pass, not assumed
— see below.

## Status against the blueprint

| Blueprint area | Status |
|---|---|
| §3 API Gateway Layer — FastAPI (Python), separate from the web UI | **Real, as of this pass.** `apps/api/` is a working FastAPI service; the Next.js app has no backend logic left. |
| §4 Custom AI / Grok / Gemma 4 as providers | **Real** — all three ported to Python, interface-uniform, correct fallback. Custom AI displays as "Oxygen AI" in the UI (a branding choice, not a blueprint requirement — checked both passages, neither specifies it). None live-tested (needs your keys). Custom AI's "orchestrating agent" / tool-calling half is not built. |
| §4.5 Provider Router — circuit breaker, health checks, model registry | **Not built.** Current fallback is one check, not the circuit-breaker-with-cooldown or persisted health-check history the blueprint specifies. |
| §4.6 Multi-provider comparison | **Real**, now in Python using the blueprint's own literal mechanism — `asyncio.gather(..., return_exceptions=True)` (Passage 1 §4.5), not a JS equivalent of it. 5 of 8 scoring axes computed for real; 3 render as the blueprint's own specified "not enough data yet" placeholder. Persists to the corrected `ai_comparisons`/`ai_comparison_results` schema. UI has a Compare mode with a side-by-side grid; still missing several Passage 4 §3.6 details (Key evidence, Market data used, Data freshness, the 6 footer actions, active-model badge). |
| §5 C++/CUDA performance layer | **Not built** — correctly so, per §5.2/5.3: nothing has been profiled as a bottleneck, and MVP may stay in the higher-level language by design. |
| §6 Deterministic trading engine | **Partial**, now genuinely Python + the specified baseline. SMA/RSI/ATR only, of MACD/Stochastic/ADX/Bollinger/VWAP. No regime-awareness classifier, no market-structure/S-R detection. |
| §7 RAG & knowledge system | **Not built.** |
| §8 Memory architecture | **Not built** at the app level (schema exists; nothing reads/writes it). |
| §9 Database | **Schema real and verified, wired to the app** — now from Python via `asyncpg`, not `pg` from Node. Same schema, same verified behavior, reconfirmed against FastAPI directly this pass. |
| §10 API architecture | `POST /api/ai/analyze` — corrected to the blueprint's actual path (was `/api/analyze`), handling both single and multi-provider modes. Still 1 of ~7 named endpoints. |
| Admin panel, charting, voice, security, error center, backtesting, compliance (Passage 2) | **Not built.** |
| Infra, CI, broader testing (Passage 3) | **Not built.** |

## Verified before delivery (actually run, not claimed)

**Python backend:**
```
$ pip install -r requirements.txt          → clean install, all imports resolve
$ mypy app --ignore-missing-imports        → 1 real error found (int/float
                                              mismatch in the synthetic-data
                                              generator) — fixed, then clean
$ pytest tests/ -v                          → 28/28 pass
$ uvicorn app.main:app --port 8000, then:
   GET  /health                             → 200
   POST /api/ai/analyze {provider: mock}    → 200, real indicator-driven result
   POST /api/ai/analyze {}                  → 400, same {error, details} shape
                                               the Next.js/Zod version returned
   POST /api/ai/analyze {providers:[mock,grok,gemma]}
                                              → 200, mock ok / grok+gemma
                                               unavailable with independent
                                               reasons — isolation confirmed
   POST /api/ai/analyze {providers:[mock,custom,grok]}
                                              → custom's unavailable reason
                                               correctly names its own three
                                               missing env vars, distinct
                                               from grok's
```

**Database, from Python via asyncpg (fresh Postgres 16 + pgvector instance):**
```
   2x single-analyze calls, same symbol      → market_data stayed at 60 rows,
                                               not 120 — bars genuinely reused
   comparison call                            → ai_comparisons.provider_set
                                               correct jsonb; ai_comparison_results
                                               has exactly 3 rows (mock+grok+gemma,
                                               all seeded so the FK holds);
                                               trading_signals has exactly 3 rows
                                               total across all calls in this
                                               session, matching how many actually
                                               found a setup — not one per call
```

**Cross-language check, beyond what was asked:** generated the same
symbol's synthetic bars in both the old TypeScript version and the new
Python port and diffed them directly. They differ — not a bug in either,
but a genuine finding: the LCG multiplies numbers that exceed
`Number.MAX_SAFE_INTEGER`, so JavaScript's float64 arithmetic silently
loses precision on every step (confirmed: `2147483647 * 1103515245` —
JS gives `...698600`, Python's arbitrary-precision integers give the
exact `...698515`). Both sequences are internally deterministic; neither
represents real data; the TypeScript version is deleted as of this pass,
so the divergence has no live consequence — recorded here because it
was checked, not assumed away.

**Integration — both services running together, real cross-origin request:**
```
   OPTIONS /api/ai/analyze, Origin: http://localhost:3000
                                              → 200, access-control-allow-origin:
                                               http://localhost:3000 present
   POST    /api/ai/analyze, Origin: http://localhost:3000
                                              → 200, same CORS header present
                                               on the actual response too
                                               (browsers check both)
   NEXT_PUBLIC_API_BASE_URL build-time inlining
                                              → proved explicitly: rebuilt with
                                               a distinctive test value, grepped
                                               the compiled client bundle,
                                               confirmed it appears there —
                                               not inferred from reading code
$ npm run build (frontend)                   → compiled successfully, 2 routes
                                               (down from 3 — /api/analyze is gone)
```

**Bug found and fixed during this pass, in both languages:** the
"deterministic for the same symbol" test compared full bar objects
including `timestamp`, which is anchored to wall-clock `now()` and can
legitimately differ by microseconds between two separate calls.
JavaScript's millisecond-resolution `Date.now()` made this pass reliably
by luck; Python's microsecond-resolution `datetime.now()` exposed it
directly. Fixed in both: the test now compares OHLCV fields only.

Not verified: a live call to any of the three real providers — blocked
by this sandbox's network policy, not by anything in the code.

## If you want to keep going

Roughly in priority order: (1) get one real provider live-tested with a
real key, (2) swap the synthetic bar generator for a real market-data
vendor, (3) fill out the indicator list, (4) the remaining Passage 4
§3.6 comparison-UI details (footer actions, evidence, freshness, model
badge), (5) circuit breaker + persisted health checks for the provider
router, (6) everything else in the status table above.
OXYGEN_AI_FILE_EOF

echo '  modified: app/page.tsx'
cat > app/page.tsx << 'OXYGEN_AI_FILE_EOF'
"use client";

import { useState, type FormEvent } from "react";
import type { ComparisonResponse, ComparisonSlot, TradeAnalysis } from "@/lib/types";

const COMPARABLE_PROVIDERS = [
  { id: "mock", label: "Mock" },
  { id: "custom", label: "Oxygen AI" },
  { id: "grok", label: "Grok" },
  { id: "gemma", label: "Gemma 4" },
];

export default function Home() {
  const [symbol, setSymbol] = useState("");
  const [mode, setMode] = useState<"single" | "compare">("single");
  const [providerId, setProviderId] = useState("mock");
  const [compareIds, setCompareIds] = useState<string[]>(["mock", "grok"]);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<TradeAnalysis | null>(null);
  const [comparison, setComparison] = useState<ComparisonResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  function toggleCompareId(id: string) {
    setCompareIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : prev.length >= 3 ? prev : [...prev, id]
    );
  }

  async function runRequest(e: FormEvent) {
    e.preventDefault();
    if (!symbol.trim()) return;
    if (mode === "compare" && (compareIds.length < 2 || compareIds.length > 3)) {
      setError("Pick 2 or 3 providers to compare");
      return;
    }
    setLoading(true);
    setError(null);
    setResult(null);
    setComparison(null);
    try {
      const body =
        mode === "single"
          ? { symbol: symbol.trim(), provider: providerId }
          : { symbol: symbol.trim(), providers: compareIds };
      const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
      const res = await fetch(`${apiBase}/api/ai/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) setError(data.error || "Request failed");
      else if (mode === "single") setResult(data);
      else setComparison(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Network error");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen bg-[#0A0C10] px-4 py-10 text-[#E8EAED]">
      <div className={mode === "compare" ? "mx-auto max-w-4xl" : "mx-auto max-w-xl"}>
        <header className="mb-8">
          <p className="font-mono text-xs uppercase tracking-widest text-[#4FD1C5]">
            Oxygen AI — minimal slice
          </p>
          <h1 className="mt-1 text-2xl font-semibold">Deterministic engine, AI reasoning</h1>
          <p className="mt-2 text-sm text-[#7C8591]">
            Indicators are computed by the API, not by the model — it only reasons over them. Price
            data below is synthetic; see README.
          </p>
        </header>

        <div className="mb-4 flex gap-1 rounded-md border border-[#232830] bg-[#12151B] p-1 text-sm">
          <ModeTab active={mode === "single"} onClick={() => setMode("single")} label="Single" />
          <ModeTab active={mode === "compare"} onClick={() => setMode("compare")} label="Compare" />
        </div>

        <form onSubmit={runRequest} className="flex flex-wrap items-start gap-2">
          <input
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            placeholder="e.g. RELIANCE"
            className="flex-1 rounded-md border border-[#232830] bg-[#12151B] px-3 py-2 font-mono text-sm outline-none focus:border-[#4FD1C5] focus:ring-1 focus:ring-[#4FD1C5]"
          />

          {mode === "single" ? (
            <select
              value={providerId}
              onChange={(e) => setProviderId(e.target.value)}
              className="rounded-md border border-[#232830] bg-[#12151B] px-2 py-2 text-sm outline-none focus:border-[#4FD1C5]"
            >
              <option value="mock">Mock (offline)</option>
              <option value="custom">Oxygen AI</option>
              <option value="gemma">Gemma 4</option>
              <option value="grok">Grok</option>
            </select>
          ) : (
            <div className="flex flex-wrap gap-2">
              {COMPARABLE_PROVIDERS.map((p) => (
                <label
                  key={p.id}
                  className={`cursor-pointer rounded-md border px-2 py-2 font-mono text-xs ${
                    compareIds.includes(p.id)
                      ? "border-[#4FD1C5] text-[#4FD1C5]"
                      : "border-[#232830] text-[#7C8591]"
                  }`}
                >
                  <input
                    type="checkbox"
                    className="mr-1.5"
                    checked={compareIds.includes(p.id)}
                    onChange={() => toggleCompareId(p.id)}
                  />
                  {p.label}
                </label>
              ))}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="rounded-md bg-[#4FD1C5] px-4 py-2 text-sm font-medium text-[#0A0C10] transition hover:opacity-90 disabled:opacity-50"
          >
            {loading ? "Analyzing…" : mode === "single" ? "Analyze" : "Compare"}
          </button>
        </form>

        {error && (
          <div className="mt-6 rounded-md border border-[#F87171]/40 bg-[#F87171]/10 px-4 py-3 text-sm text-[#F87171]">
            {error}
          </div>
        )}

        {result && <ResultCard result={result} />}
        {comparison && <ComparisonGrid data={comparison} />}
      </div>
    </main>
  );
}

function ModeTab({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex-1 rounded px-3 py-1.5 font-mono text-xs uppercase tracking-wide transition ${
        active ? "bg-[#4FD1C5] text-[#0A0C10]" : "text-[#7C8591] hover:text-[#E8EAED]"
      }`}
    >
      {label}
    </button>
  );
}

function ResultCard({ result }: { result: TradeAnalysis }) {
  const noSetup = result.status === "NO_VALID_SETUP";
  const dirColor =
    result.direction === "LONG" ? "#34D399" : result.direction === "SHORT" ? "#F87171" : "#FBBF24";

  return (
    <div className="mt-6 rounded-lg border border-[#232830] bg-[#12151B] p-5">
      <div className="flex items-center justify-between">
        <span
          className="rounded px-2 py-1 font-mono text-xs font-semibold uppercase"
          style={{ color: dirColor, backgroundColor: `${dirColor}1A` }}
        >
          {noSetup ? "No valid setup" : result.direction}
        </span>
        {result.confidence !== null && (
          <span className="font-mono text-sm text-[#7C8591]">
            confidence <span className="text-[#E8EAED]">{result.confidence}</span>
          </span>
        )}
      </div>

      {!noSetup && (
        <dl className="mt-4 grid grid-cols-2 gap-3 font-mono text-sm sm:grid-cols-4">
          <Field label="entry" value={result.entry} />
          <Field label="stop" value={result.stopLoss} />
          <Field label="target 1" value={result.targets[0]} />
          <Field label="target 2" value={result.targets[1]} />
        </dl>
      )}

      <p className="mt-4 text-sm leading-relaxed text-[#C7CCD3]">{result.reasoningSummary}</p>

      {result.supportingEvidence.length > 0 && (
        <ul className="mt-3 space-y-1 font-mono text-xs text-[#7C8591]">
          {result.supportingEvidence.map((ev, i) => (
            <li key={i}>· {ev}</li>
          ))}
        </ul>
      )}

      <div className="mt-5 flex flex-wrap gap-x-4 gap-y-1 border-t border-[#232830] pt-3 font-mono text-[11px] text-[#5B6470]">
        <span>source: {result.source}</span>
        <span>provider: {result.provider}</span>
        <span>model: {result.model}</span>
        <span>rr: {result.riskReward ?? "n/a"}</span>
        <span>persisted: {result.persisted ? "yes" : "no"}</span>
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: number | null | undefined }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-[#5B6470]">{label}</dt>
      <dd className="text-[#E8EAED]">{value ?? "—"}</dd>
    </div>
  );
}

function ComparisonGrid({ data }: { data: ComparisonResponse }) {
  return (
    <div className="mt-6">
      <p className="mb-3 font-mono text-[11px] text-[#5B6470]">
        symbol: {data.symbol} · persisted: {data.persisted ? "yes" : "no"}
      </p>
      <div
        className="grid gap-3"
        style={{ gridTemplateColumns: `repeat(${data.results.length}, minmax(0, 1fr))` }}
      >
        {data.results.map((slot) => (
          <ComparisonColumn key={slot.providerId} slot={slot} />
        ))}
      </div>
    </div>
  );
}

function ComparisonColumn({ slot }: { slot: ComparisonSlot }) {
  if (slot.outcome === "unavailable") {
    return (
      <div className="rounded-lg border border-[#232830] bg-[#12151B] p-4">
        <p className="font-mono text-xs uppercase tracking-widest text-[#5B6470]">{slot.providerId}</p>
        <p className="mt-3 inline-block rounded bg-[#FBBF24]/10 px-2 py-1 font-mono text-[11px] uppercase text-[#FBBF24]">
          unavailable
        </p>
        <p className="mt-2 text-[11px] leading-relaxed text-[#5B6470]">{slot.reason}</p>
      </div>
    );
  }

  const { analysis, scores } = slot;
  const noSetup = analysis.status === "NO_VALID_SETUP";
  const dirColor =
    analysis.direction === "LONG" ? "#34D399" : analysis.direction === "SHORT" ? "#F87171" : "#FBBF24";

  return (
    <div className="rounded-lg border border-[#232830] bg-[#12151B] p-4">
      <p className="font-mono text-xs uppercase tracking-widest text-[#4FD1C5]">{slot.providerId}</p>
      <span
        className="mt-2 inline-block rounded px-2 py-1 font-mono text-[11px] font-semibold uppercase"
        style={{ color: dirColor, backgroundColor: `${dirColor}1A` }}
      >
        {noSetup ? "No setup" : analysis.direction}
      </span>

      {!noSetup && (
        <dl className="mt-3 space-y-1 font-mono text-xs">
          <ScoreRow label="entry" value={analysis.entry} />
          <ScoreRow label="stop" value={analysis.stopLoss} />
          <ScoreRow label="target 1" value={analysis.targets[0]} />
          <ScoreRow label="confidence" value={analysis.confidence} />
          <ScoreRow label="r:r" value={analysis.riskReward} />
        </dl>
      )}

      <p className="mt-3 text-xs leading-relaxed text-[#C7CCD3]">{analysis.reasoningSummary}</p>

      <div className="mt-3 space-y-0.5 border-t border-[#232830] pt-2 font-mono text-[10px] text-[#5B6470]">
        <ScoreRow label="data completeness" value={scores.dataCompleteness} />
        <ScoreRow label="indicator agreement" value={scores.indicatorAgreement} />
        <ScoreRow label="risk/reward quality" value={scores.riskRewardQuality} />
        <ScoreRow label="rule compliance" value={scores.ruleCompliance} />
        <ScoreRow label="explanation consistency" value={scores.explanationConsistency} />
        <p className="flex justify-between">
          <span>historical validation</span>
          <span>not enough data</span>
        </p>
        <p className="flex justify-between">
          <span>prediction outcome</span>
          <span>not enough data</span>
        </p>
        <p className="flex justify-between">
          <span>confidence calibration</span>
          <span>not enough data</span>
        </p>
      </div>
    </div>
  );
}

function ScoreRow({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="flex justify-between">
      <dt className="text-[#5B6470]">{label}</dt>
      <dd className="text-[#E8EAED]">{value ?? "—"}</dd>
    </div>
  );
}
OXYGEN_AI_FILE_EOF

echo '  modified: lib/types.ts'
cat > lib/types.ts << 'OXYGEN_AI_FILE_EOF'
/**
 * Frontend-only type definitions. Runtime validation (formerly Zod here)
 * now happens once, server-side, in apps/api/app/schemas.py (Pydantic) --
 * the actual API Gateway per Passage 1 §3. These are plain TypeScript
 * types for rendering the response the frontend receives over HTTP; they
 * describe the same contract, they don't re-validate it.
 */

export interface IndicatorBundle {
  sma20: number | null;
  sma50: number | null;
  rsi14: number | null;
  atr14: number | null;
  lastClose: number;
  trend: "up" | "down" | "flat";
}

export interface TradeAnalysis {
  status: "SETUP_FOUND" | "NO_VALID_SETUP";
  direction: "LONG" | "SHORT" | null;
  entry: number | null;
  stopLoss: number | null;
  targets: number[];
  confidence: number | null;
  riskReward: number | null;
  reasoningSummary: string;
  supportingEvidence: string[];
  source: "mock" | "hosted_api" | "local_model";
  provider: string;
  model: string;
  indicatorsUsed: IndicatorBundle;
  generatedAt: string;
  persisted: boolean;
}

export interface ScoreBreakdown {
  dataCompleteness: number | null;
  indicatorAgreement: number | null;
  riskRewardQuality: number | null;
  ruleCompliance: number | null;
  explanationConsistency: number | null;
  historicalValidation: "not_enough_data";
  predictionOutcome: "not_enough_data";
  confidenceCalibration: "not_enough_data";
}

export type ComparisonSlot =
  | { outcome: "ok"; providerId: string; analysis: TradeAnalysis; scores: ScoreBreakdown }
  | { outcome: "unavailable"; providerId: string; reason: string };

export interface ComparisonResponse {
  mode: "multi";
  symbol: string;
  results: ComparisonSlot[];
  persisted: boolean;
  generatedAt: string;
}
OXYGEN_AI_FILE_EOF

echo '  modified: package.json'
cat > package.json << 'OXYGEN_AI_FILE_EOF'
{
  "name": "oxygen-ai-web",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "start": "next start",
    "typecheck": "tsc --noEmit"
  },
  "dependencies": {
    "next": "^14.2.5",
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "typescript": "^5.5.4",
    "@types/node": "^20.14.0",
    "@types/react": "^18.3.3",
    "@types/react-dom": "^18.3.0",
    "tailwindcss": "^3.4.7",
    "postcss": "^8.4.40",
    "autoprefixer": "^10.4.19"
  }
}
OXYGEN_AI_FILE_EOF

echo ""
echo "Done. package-lock.json will regenerate automatically on npm install."
echo ""
echo "Next steps:"
echo "  cd apps/api && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
echo "  .venv/bin/mypy app --ignore-missing-imports"
echo "  .venv/bin/pytest tests/ -v"
echo "  .venv/bin/uvicorn app.main:app --reload --port 8000   # terminal 1"
echo "  cd .. && npm install && npm run build && npm start     # terminal 2"