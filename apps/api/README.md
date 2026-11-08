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

## Authentication (JWT is required)

`POST /api/ai/analyze` and the comparison-feedback endpoint require a
bearer token (Passage 4 §6.2). Setup, once per environment:

```bash
# 1. In apps/api/.env.local: a signing secret (never commit it) and a database
python -c "import secrets; print(secrets.token_urlsafe(48))"   # -> JWT_SECRET=
# DATABASE_URL=postgresql://...   (sessions live in Postgres)

# 2. Apply every migration in order (0005 adds the session indexes; 0006 de-duplicates and
#    constrains technical_indicators and repairs ai_models display names — idempotent)
for f in ../../infra/migrations/000*.sql; do psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"; done

# 3. Create the first admin — registration only ever yields the `user` role
python -m app.cli create-admin --email you@example.com      # prompts for the password
python -m app.cli set-role --email someone@example.com --role admin
```

Operational behaviour added in P2 (Passage 1 §9, Passage 4 §5.2–§5.3):

- **Retention.** Expired `sessions` rows and expired short-lived `memories` rows are
  deleted by `app/db/retention.py` — every `RETENTION_INTERVAL_SECONDS` (default
  3600, `0` disables) inside the API process, or on demand with
  `python -m app.cli purge-expired`. `audit_logs` and `trade_results` are never
  purged; long-term memory (`expires_at IS NULL`) is never purged.
- **Audit trail.** Knowledge upload/delete, manual tool invocations and CLI role
  changes write `audit_logs` rows (`db/audit.py`). Writes are best-effort and
  never fail the action they describe. Note `audit_logs.actor_id` is
  `ON DELETE RESTRICT` by specification, so an account that has an audit trail
  cannot be hard-deleted.
- **Error Center feed.** An unhandled exception returns `errorId`; the same id is
  the `error_id` of a durable `error_logs` row (type, message, stack, route, user).
- **Provider health history.** `POST /api/ai/{id}/test` and `GET /api/ai/{id}/health`
  each append an `ai_health_checks` row (`ok` / `down`).
- **Circuit breaker.** A provider that is simply *not configured* is no longer
  counted as a failure, so it reports `not_configured` instead of drifting to
  `circuit_open`.

Roles: `admin` can upload/delete knowledge documents, call provider
`/test` and `/health`, and use the tool endpoints; any signed-in user can
analyse, search/list knowledge, and rate their own comparisons.

`AUTH_REQUIRED=false` re-enables anonymous analysis for local development
only (the API logs a warning at startup). Behind a reverse proxy set
`TRUSTED_PROXY_COUNT` (and start uvicorn with `--no-proxy-headers`) so rate
limits key off the real client address — see `app/security/network.py`.

## Knowledge base maintenance

Earlier versions stored an all-zero embedding for any chunk with no ASCII
letters or digits (for example a Bengali or Hindi document). Cosine
similarity against a zero vector is NaN, which made `POST
/api/knowledge/search` fail with `Out of range float values are not JSON
compliant: nan`. New code cannot write such rows and search skips any that
remain, but a skipped row is also unsearchable. To repair rows already in your
database:

```bash
cd apps/api
python -m app.rag.maintenance reembed-unusable --dry-run   # report only
python -m app.rag.maintenance reembed-unusable             # re-embed (or drop, if nothing embeddable)
python -m app.rag.maintenance reembed-unusable --document-id <uuid>   # one document
```

It is safe to re-run. It uses whichever embedder is configured now and does
not touch healthy rows.
