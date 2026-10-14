# Endpoint reconciliation — Passage 1 §10 + Passage 4 §6.1–§6.3

Built from the **live route table** of the application (`app.routes`), then compared with
the endpoints Passage 1/4 name. Updated at the end of every phase.

Required contract shape (P4 §6.3): Method · Path · Authentication · Request schema ·
Response schema · Error schema · Rate limits · Permissions. Errors everywhere use the single
body `{ "error", "code", "detail", "details"?, "errorId"? }` (`app/errors.py`).

Rate-limit buckets (`app/rate_limit.py`, per user when authenticated, else per validated
client IP): `auth` 10/min · `analyze` 30/min · `admin` 60/min (models, route, provider
test/health) · `default` 120/min.

## A. Implemented (18 routes)

| Method · Path | Request → Response | Authn / Authz | Validation & errors | Persistence · deps | Status · Tests |
|---|---|---|---|---|---|
| GET `/health` | — → `{status, service, databaseConfigured, authConfigured, authRequired, nativeLayerAvailable}` | none | — | — | Done · smoke-run against real uvicorn; automated test planned (P2) |
| POST `/api/auth/register` | `RegisterRequest{email, password 12–256}` → `TokenResponse{accessToken, tokenType, expiresIn, user}` | none · `auth` bucket | 400 invalid **or email already registered** · 503 auth not configured | `users`, `sessions` (IP validated) | Done · integration ✔ |
| POST `/api/auth/login` | `LoginRequest` → `TokenResponse` | none · `auth` bucket | 401 identical for unknown/wrong/inactive; constant-time | `sessions` | Done · integration ✔, timing-decoy ✔ |
| POST `/api/auth/logout` | — → `{ok}` | JWT (scheme case-insensitive) | idempotent | deletes `sessions` row | Done · integration ✔ (3 scheme casings) |
| GET `/api/auth/me` | — → `UserResponse` | JWT | 401 · 503 store down | reads `users` | Done · integration ✔ |
| POST `/api/ai/analyze` | `AnalyzeRequest{symbol≤20, mode?, provider?, providers[2–3]?, timeframe:"1d", accountEquity?…}` → `TradeAnalysis` (single) \| `ComparisonResponse` (multi) | **JWT required** (`AUTH_REQUIRED=false` = dev only) · `analyze` bucket | 400 invalid · 401 · **409 market data unavailable/invalid** · 503 provider unavailable (only if the fallback also fails) · a verification failure is a 200 `NO_VALID_SETUP` | `market_data`, `technical_indicators`, `trading_signals`, `usage_logs`, `conversations`+`ai_comparisons`+`ai_comparison_results` (multi) · engine, RAG, router | Done* · HTTP auth ✔; engine/router unit ✔. *Array-shape question D4 → P3/P12 |
| PATCH `/api/ai/comparisons/{id}` | `RateComparisonRequest{rating 1–5?, preferredProviderId?}` → `ComparisonFeedback` | JWT required · **owner or admin only** (non-owner gets 404) | 400 malformed id · 404 | `ai_comparisons` | Done · integration ✔ |
| GET `/api/ai/providers` | — → provider list | **none (public)** — reveals which providers are configured; tighten decision → P12 | — | registry | Done · no HTTP test (P12) |
| POST `/api/ai/route` | `RouteRequest{mode?, provider?, providers?, allowFallback}` → `RouteResponse{mode, decisions[]}` | JWT (any role) · `admin` bucket | 400 | registry + breaker (no execution) | Done · integration ✔ |
| GET `/api/ai/tools` | — → tool list | JWT + **admin** | 401/403 | `tool_registry` | Done · registry-level tests (HTTP test → P6) |
| POST `/api/ai/tools/{name}/invoke` | tool input → tool output | JWT + **admin** | 404 unknown tool. Tool failures are **HTTP 200** with the outcome in the body: `{tool, status: ok\|error\|timeout, output, error, latencyMs, available}`; runs as `provider_id="custom"` for permission checks | `tool_calls` | Done · registry-level ✔ (HTTP test → P6) |
| POST `/api/ai/{provider_id}/test` | — → `HealthResponse{ok, latencyMs, modelChecked, detail, transport}` | JWT + **admin** · `admin` bucket | 404 unknown provider | provider call | Done* · admin gating ✔. *P4 §6.2 wants `{ok, latency_ms, model_checked}` → snake_case alias in P3 |
| GET `/api/ai/{provider_id}/health` | — → `HealthResponse` | JWT + **admin** · `admin` bucket | as above | provider call; `ai_health_checks` write → P2 | Done* · same note |
| POST `/api/knowledge/upload` | `KnowledgeUploadRequest{title, content, sourceType}` | JWT + **admin** | 400 · 403 | `knowledge_documents/chunks`, `embeddings` | Done · authz ✔ |
| POST `/api/knowledge/upload/file` | multipart PDF/TXT/CSV | JWT + **admin** | 400 unsupported · size limit (**reads whole file before checking** → P9) | same | Done · authz ✔ |
| POST `/api/knowledge/search` | `KnowledgeSearchRequest{query, topK}` → chunks | JWT (any role) | 400 | pgvector | Done · authz ✔ |
| GET `/api/knowledge/documents` | → documents | JWT (any role) | — | — | Done · authz ✔ |
| DELETE `/api/knowledge/documents/{id}` | — | JWT + **admin** | 404 | cascades chunks/embeddings | Done · authz ✔ |

**Spec ambiguity recorded (not silently resolved):** P4 §6.2 lists "409 market data unavailable →
engine returns NO_VALID_SETUP, not an HTTP error". Those two halves conflict. The baseline raises
HTTP 409 when the engine cannot run at all (too few / invalid bars) and returns a 200
`NO_VALID_SETUP` when the engine ran but verification failed. Kept as-is; to be revisited in P7,
when real market-data adapters make "stale vs unavailable" a first-class distinction.

## B. Required by Passage 1/4 but not yet implemented

| Method · Path | Source | Spec detail | Phase |
|---|---|---|---|
| GET/POST/PATCH/DELETE `/api/ai/models` | P1 §10, P4 §6.2 | CRUD over `ai_models`, admin RBAC, **every write → `audit_logs`**, 7-value status enum | P3 |
| POST `/api/ai/compare` | P1 §10 | accepts 2–3 `provider_id`; writes `ai_comparisons` + `ai_comparison_results` | P3 |
| POST `/api/ai/gemma/test`, GET `/api/ai/gemma/health` | P1 §10 | generic routes already serve the *path*; add `{ok, latency_ms, model_checked}` and hosted+local health backed by `ai_health_checks` | P3/P4 |
| Custom AI test/health | P4 §6.3 category | same generic routes; local-runtime detail | P4 |
| Provider settings read/write | P1 §4.4.1 | the 14 local-runtime admin fields, timeouts, retries, fallback model | P3/P4 |
| Market Data (quotes, history, instruments) | P4 §6.3 | via adapter abstraction | P7 |
| Indicators (list/compute; custom-indicator CRUD + test) | P4 §6.3, G01b | constrained expression tree; backtest-before-enable | P8 |
| Memory (user-scoped get/put/delete/export) | P1 §8 | explicit delete/export for long-term + session | P10 |
| WebSocket `/ws/conversations/{id}` | P1 §3 | auth, channel isolation, persistence, pub/sub | P11 |
| Users (admin CRUD) | P4 §6.3 | admin | P12 |
| Trading Signals (list/read) | P4 §6.3 | user-scoped | P12 |
| History (save/list user results) | P4 §3.6 "Save result" | per-user | P12 |
| Usage | P4 §6.3 | reads `usage_logs` | P12 |
| Monitoring (dashboards) | P4 §6.3 | reads `ai_health_checks` | P12 |
| Errors (error center) | P4 §6.3 | reads/updates `error_logs` (status workflow) | P12 |
| Admin (system settings versioned, audit log read) | P1 §8, P4 §6.3 | admin | P12 |
| Feedback | P4 §5.2 `feedback` | rating on signals | P12 |

## C. Deferred — owning subsystem/spec outside Passage 1 + 4

Backtesting (run/results), Paper Trading (positions), Voice (session), Charting data,
payments/billing, News/Sentiment. Contracts follow the same shape and are deliberately deferred
(P4 §6.3: "deferred to a Phase-1 OpenAPI-spec task rather than pre-written"). The tools that
depend on them (`backtesting`, `portfolio_analyzer`, `news`, `sentiment`, …) already return
an explicit *unavailable* result. P14 exports the OpenAPI document so the remaining
contracts can be added against a real schema.

## D. The 19 required endpoint categories (P4 §6.3)

| Category | State |
|---|---|
| Auth | ✔ |
| Users | P12 |
| AI | ✔ (analyze) · P3 (compare) |
| Grok | ✔ generic test/health · P3 alias |
| Custom AI | generic test/health ✔ · P4 |
| AI Provider Routing | ✔ `/route` |
| AI Model Management | P3 |
| Market Data | P7 |
| Indicators | P8 |
| Trading Signals | P12 |
| Backtesting | deferred (C) |
| Paper Trading | deferred (C) |
| Knowledge | ✔ |
| Memory | P10 |
| Voice | deferred (C) |
| Admin | P12 |
| Monitoring | P12 |
| Errors | P12 |
| Usage | P12 |
