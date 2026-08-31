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
