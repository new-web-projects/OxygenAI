# Oxygen AI — Passage 1 + Passage 4 Implementation Plan

Living document. Every delivery updates the **Progress** table at the bottom;
a continuation session resumes at the first phase not marked DONE — it does
not re-analyse the project from scratch.

## 0. Source of truth and its limits

| Source | Role |
|---|---|
| Passage 1 (Architecture Core) | Primary blueprint |
| Passage 4 (Correction & Recovery, G01–G16) | Corrections folded **into** Passage 1 subsystems — never a parallel system |
| Master Prompt / Work-Start constraints | Binding architecture rules |
| Uploaded `OxygenAI-main.zip` (the 46-file baseline) | Actual current state |

**Not supplied: Passage 2 and Passage 3.** Passage 1 and Passage 4 repeatedly delegate
detail to them (Admin Panel control inventory P2 §11.1, market-data vendors/DEMO-PAPER-LIVE
P2 §13, backtesting/paper trading P2 §18, voice, charting, error center, roadmap P3 §26–27).
Where a requirement's only specification lives there, this plan implements what Passage 1 /
Passage 4 / the continuation prompt state and marks the rest **"needs Passage 2/3 text"** —
it does not invent a spec. The continuation prompt also mentions "payments" as an admin
domain; no Passage 1/4 text defines it, so it is listed under *Not specified* below.

## 1. Baseline reconciliation (what the zip already contains)

The continuation prompt's "remaining" list pre-dates the uploaded zip. Verified against the
code (and by running it: 215 backend tests passing, mypy clean, native build + parity gate,
frontend build):

| Prompt listed as remaining | Actual baseline state | Remaining work → phase |
|---|---|---|
| RAG / pgvector pipeline | **Built**: extract→clean→chunk→embed→pgvector→retrieve→rerank(lexical)→labelled context | Hardening: embedding-model filter, oversize-chunk splitting, query task type, batching, rerank decision → **P9** |
| 15-tool registry + governance | **Built**: 15 tools, schemas, timeouts, rate limits, `tool_calls` logging, admin API | Role/mode permission check, provider tool-calling loop, deferred-tool owners → **P6** |
| Authentication / JWT | Built but **not enforced** (analyze accepted anonymous) | Enforcement + hardening → **P1 (DONE)** |
| Memory layers | Knowledge memory only | Short-term, session, long-term (`memories`), system (versioned), delete/export → **P10** |
| Market-data vendor adapters | **Not built** (synthetic OHLCV only) | → **P7** |
| WebSocket | **Not built** | → **P11** |
| Redis | **Not built** (`REDIS_URL` read, unused) | → **P9** |
| Custom-indicator evaluator | **Not built** (table only) | → **P8** |
| Local/offline runtimes | Gemma local *transport* only | Runtime catalog, admin config, availability, Custom AI keyless local → **P4** |
| Admin Panel UI | **Not built** | → **P5 + P13** |
| Models CRUD / named endpoints | **Not built** | → **P3, P12** |

### Passage 1 section status

| P1 § | Requirement | Baseline | Phase |
|---|---|---|---|
| §3 | REST gateway, auth, rate-limit | Built; auth/rate-limit hardened in P1 | P1 ✔ |
| §3 | WebSocket | Missing | P11 |
| §3 | Redis (cache/queue) | Missing | P9 |
| §3 | Market Data Abstraction | Missing | P7 |
| §4.1 | Custom AI as orchestrating agent | Provider only; `agent/orchestrator.py` referenced in docstrings but absent | P6 |
| §4.1 / G01b | Admin-authored custom indicators (constrained expression tree) | Missing | P8 |
| §4.2 / G02 | Grok slugs, redirects, Responses/Agent-Tools API, registry automation | Slug handling built; DB flip + alert partial; Responses API absent | P3, P6 |
| §4.3 | Gemma 4 identity vs transport | Built | — |
| §4.4 / §4.4.1 | Offline runtimes, admin fields, controlled fallback, `local_model` tag | Transport only | P4 |
| §4.5 / G03 / G11 | Router, breaker (5/60s, **admin-configurable**), fallback, `ai_health_checks` persisted | Built; configurability + persistence missing | P2, P3 |
| §4.6 / G04 / G09 / G10 | Comparison, scoring axes, placeholders, footer actions, model badge | Built; "Save → History" missing; "Why they disagree" is Post-MVP (P3 §27) | P12/P13 |
| §4.7 | Reasoning pipeline | Built | — |
| §4.8 / G05 | Tool registry governed, provider-agnostic | Registry built; not wired to providers | P6 |
| §5 | C++/CUDA layer, pybind11, parity gate, pure-Python fallback | Built and verified | — |
| §6 / G06 | Deterministic engine | Built | — |
| §7 | RAG pipeline + source tags | Built; hardening | P9 |
| §8 | Six memory layers + delete/export | 1 of 6 | P10 |
| §9 / G12 | Schema, relationships, indexing, retention | Schema complete (0001–0005); operational usage partial | P2, P10, P12 |
| §10 / G08 / G13 | Named endpoints, 19 categories, contract shape | 18 routes of the required set | P3, P12 (see ENDPOINT_CHECKLIST.md) |

### Passage 4 G01–G16

| ID | Status in baseline | Action | Phase |
|---|---|---|---|
| G01 | a: table is documentation; b: mechanism missing | Docs for (a); implement evaluator for (b) | P14 / P8 |
| G02 | Mostly built | Responses API path; DB-backed registry flip + alert | P6 / P3 |
| G03 | Built (5/60s) | Make admin-configurable | P3 |
| G04, G10, G11 | Built | Re-verify in final audit | P15 |
| G05 | Registry built, governance partial | Roles/modes, orchestration | P6 |
| G06 | Built | Re-verify | P15 |
| G07 | Rationale only needs documenting | Docs | P14 |
| G08 | `/route` built; models CRUD, compare missing | Implement | P3 |
| G09 | Built except "Save → History" | History list + save | P12/P13 |
| G12 | Schema ✔; see table below | Per-table ownership | P2/P10/P12 |
| G13 | 18 of the required endpoints | Full reconciliation | P3, P12 |
| G14, G15, G16 | Missing documentation | Docs only, no runtime change | P14 |

### G12 — table ownership (not "make every count non-zero")

| Table | Owner subsystem | Operational today? | Plan |
|---|---|---|---|
| users, roles, permissions, sessions | Auth | Yes | P1 (+purge P2) |
| ai_providers, ai_models | Router/registry | Yes (seeded on use) | P3 makes DB the registry's system of record |
| ai_provider_settings | Provider admin | No | P3/P4 |
| ai_routing_rules, ai_fallback_rules | Router | No | P3 decides: use for admin-configurable fallback, else document as dormant |
| ai_health_checks | Router monitoring | No | P2 writes, P12/P13 reads |
| ai_comparisons, ai_comparison_results | Comparison | Yes | — |
| market_instruments, market_data, technical_indicators | Market data / engine | Yes (dedupe missing) | P2 (unique+upsert), P7 |
| trading_signals, feedback | Engine / evaluation | signals yes; feedback no | P12 |
| knowledge_*, embeddings | RAG | Yes | P9 |
| conversations | Conversation/WebSocket | **Started in P1** (comparison ownership) | P11 |
| messages | WebSocket | No | P11 |
| memories | Memory | No | P10 |
| tool_registry, tool_calls | Tools | Yes | P6 |
| custom_indicator_definitions | Custom indicators | No | P8 |
| error_logs, audit_logs, usage_logs | Ops | usage partial; error/audit no | P2 |
| system_settings | System memory | No | P2/P12 |
| feature_flags | Ops | No | Dormant until an owning feature exists |
| strategies, strategy_versions, backtests, trade_setups, trade_results | Backtesting / paper trading (P2 §18) | No | **Intentionally dormant** — owner subsystem not in this scope |
| voice_settings | Voice (P2) | No | **Intentionally dormant** |
| Retention | audit_logs/trade_results indefinite; sessions/memories expire; tick roll-up N/A (no tick data) | — | P2 job |

### Not specified in the supplied sources (not implemented, not forgotten)

Backtesting, paper trading, voice, charting, payments/billing, news/sentiment feeds,
Post-MVP "Why they disagree". Their endpoints are listed in `ENDPOINT_CHECKLIST.md` with
status *deferred — owner subsystem/spec outside Passage 1+4*. The tool registry already
reports the dependent tools honestly as unavailable.

## 2. Cross-cutting decisions (flag to the owner)

| # | Decision | Rationale |
|---|---|---|
| D1 | `AUTH_REQUIRED` defaults **true**; `false` is an explicit dev escape hatch with a startup warning | P4 §6.2: analyze "Auth: required (JWT)" |
| D2 | Knowledge upload/delete are **admin-only**; search/list need login | P1 §8 "until admin deletes"; uploads feed every user's prompts |
| D3 | JWT kept in `sessionStorage` | httpOnly-cookie auth needs CSRF work — separate change |
| D4 | P1 §10 says analyze returns an **array**; baseline returns an object (frontend depends on it). Not changed in P1; resolved in P3/P12 via the named `POST /api/ai/compare` and an opt-in array shape | Avoids a silent breaking change; spec still satisfied by named endpoint |
| D5 | `normalize_confidence(1)` → 100 is pinned by an existing test but ambiguous on a 0–100 prompt | Owner call; untouched |
| D6 | R:R is a constant 2.0 for ~93% of setups (fixed R-multiples) — tests pin it | Design observation; untouched |
| D7 | Legacy `apply_*.sh` scripts overwrite current files with older versions | Recommend deleting; P14 adds a guard |
| D8 | `npm audit` flags `next@14.2.35`; fix needs a major bump | Separate decision, not made silently |

## 3. Phases

Order is dependency-driven. Where G15's rationale (Admin Panel earlier, Voice later) applies,
the Admin Panel is split: provider/model administration (P5) lands as soon as its APIs exist.

### P1 — Gateway security & JWT enforcement — **DONE**
- **Objective:** make auth real; harden the gateway the rest of the system sits behind.
- **P1 §:** 3 (auth, rate-limit), 8 (admin-curated knowledge). **P4:** G08b (analyze auth required, rate-limit per plan, error contract), G12 (sessions/conversations usage).
- **Depends on:** baseline only.
- **Files:** `security/{network,dependencies}.py`, `routers/{auth,analyze,comparisons,knowledge}.py`, `rate_limit.py`, `errors.py`, `observability.py`, `main.py`, `config.py`, `cli.py`, `db/{conversations,comparisons,users}.py`, frontend `lib/api.ts`, `app/components/AuthPanel.tsx`, `app/page.tsx`, tests.
- **DB:** `0005_auth_hardening.sql` (indexes only, idempotent). **API:** auth now enforced; ownership on comparison feedback; knowledge RBAC. **Frontend:** login/register/sign-out gate.
- **Tests / Verification / Completion:** see PHASE1 report in the delivery; 280 backend + 13 client tests; all green.

### P2 — Persistence hygiene & operational foundations
- **Objective:** fix the correctness/ops defects later phases build on; give G12's operational tables their writers.
- **P1 §:** 4.5 (health checks persisted), 9 (indexing, retention). **P4:** G12 (b,c,d), G03.
- **Depends on:** P1.
- **Work:** `technical_indicators` unique key + upsert (duplicate rows per request today); `audit_logs` writer; `error_logs` persistence from the 500 handler (error id ↔ row); `ai_health_checks` writes from `/health` probes; usage logging in multi mode; mock-vs-fallback `display_name` corruption; breaker must not count "not configured" as a failure; negative-stop guard (`stop>0`); `get_pool` race + `close_pool` on shutdown; retention job (expired sessions, expired memories).
- **Files:** `db/{indicators_db,audit,errors,health,usage,retention}.py`, `providers/{circuit_breaker,registry}.py` (minimal), `engine/risk_engine.py`/`verify.py` (guard), `main.py` lifespan.
- **DB:** `0006_*` (unique index on technical_indicators with dedupe of existing rows; partial index `error_logs(status) WHERE status!='CLOSED'`). **API:** none. **Frontend:** none.
- **Tests:** DB integration for each writer; breaker config-error regression; retention. **Verify:** full suite + mypy. **Complete when:** every item has a failing-before/passing-after test.

### P3 — Provider settings, model registry persistence, Models CRUD, named endpoints
- **Objective:** DB becomes the registry's system of record; admins manage models/providers through the API.
- **P1 §:** 4.2, 4.5, 9.1, 10. **P4:** G02g, G03 (admin-configurable breaker), G08a/b, G12.
- **Depends on:** P2 (audit writer).
- **Work:** registry loads/syncs `ai_models` + seven-value status enum; `GET/POST/PATCH/DELETE /api/ai/models` (admin, audited); `ai_provider_settings` read/write incl. encrypted API keys (env stays authoritative unless admin override set; master key via env, never invented); breaker thresholds from settings; retired-slug auto-flip + alert persisted; `POST /api/ai/compare` (2–3 providers); snake_case `latency_ms`/`model_checked` aliases on test/health; decide `ai_routing_rules`/`ai_fallback_rules`.
- **DB:** only if CHECK/enum or new column needed (decided after inspecting 0001). **Frontend:** none (P5).
- **Tests:** CRUD authz matrix, audit rows, encryption round-trip, registry reload, compare endpoint parity with analyze multi.

### P4 — Local/offline runtimes (Custom AI + Gemma 4)
- **Objective:** real local operation, not just a transport.
- **P1 §:** 4.1, 4.3.1, 4.4, 4.4.1. **P4:** G02 (Gemma list), G03 (both Gemma transports).
- **Depends on:** P3 (settings + registry persistence).
- **Work:** runtime catalog (llama.cpp, MLX, LiteRT-LM [E2B/E4B only], vLLM, Ollama; others listed as alternates) as descriptors over one OpenAI-compatible local transport; the 14 admin fields persisted (path, runtime, host, port, model id, quantization, GPU/CPU, context, enable, offline toggle, health, availability, resource display, fallback); reachability probe → controlled fallback/error; always tag `local_model`; Custom AI keyless when local; Gemma hosted gets timeout/retry parity; Gemma stays "Gemma 4", never "Gemini".
- **Tests:** against an in-process OpenAI-compatible fake server (mock-tested; live runtime verification blocked — no runtime/GPU/weights in the sandbox, stated plainly).

### P5 — Admin Panel foundation + provider/model/runtime administration (increment A)
- **Objective:** the UI surface to test P3/P4 against (G15 rationale).
- **P1 §:** 4.4.1 (admin requirement list). **P4:** G15, G09 (active-model badge source).
- **Depends on:** P3, P4. **Files:** `app/admin/**`, shared admin client. **API:** none new.
- **Work:** role-gated admin shell; providers, models CRUD, provider settings, local-runtime form (14 fields), health/test buttons, usage widget reading the account's real quota where the API exposes it.
- **Verify:** typecheck, build, client tests; **no browser available in the sandbox** — stated in the report.

### P6 — Tool-calling orchestration & tool governance
- **Objective:** providers actually use the governed registry.
- **P1 §:** 4.1, 4.2, 4.8. **P4:** G05, G02c.
- **Depends on:** P3. **Work:** `agent/orchestrator.py` provider-agnostic tool loop (bounded steps/timeouts); role + mode permission check at execution; every call logged; Grok Responses/Agent-Tools path as an option; deferred tools stay honest about owners.
- **Tests:** loop with scripted fake providers; permission denial; step-limit; log rows.

### P7 — Market-data abstraction & adapters
- **P1 §:** 3, 6. **P4:** G06 (data source), §9 URLs. **Depends on:** P2. **Needs P2 §13 text for DEMO/PAPER/LIVE semantics** (column `data_mode` exists).
- **Work:** `market_data/` package: adapter interface, registry, demo (synthetic, labelled), TrueData and Global Datafeeds adapters (**unverified against live vendor APIs** — no credentials), crypto/forex adapter slot; engine reads via the abstraction; staleness; Market Data endpoints; market/historical tools use it.
- **Tests:** fixtures + error/fallback paths; live vendor verification blocked by credentials.

### P8 — Custom-indicator expression-tree evaluator
- **P1 §:** 4.1. **P4:** G01b. **Depends on:** P7.
- **Work:** constrained, side-effect-free expression tree over OHLCV + base indicators (no code execution), versioning, admin-authored, backtest-before-enable test endpoint, engine/prompt integration, CRUD endpoints.

### P9 — Redis infrastructure + RAG hardening
- **P1 §:** 3, 7, 8. **P4:** G07 (Redis role). **Depends on:** P1.
- **Work:** Redis client + health; shared rate-limit counters; cache; session-memory store; pub/sub primitive for P11; (real Redis available via apt in the sandbox). RAG: filter by `model_used`, split oversize chunks (a 20k-row CSV currently becomes one 180k-word chunk), `RETRIEVAL_QUERY` task type, embedding batching, rerank decision.

### P10 — Memory layers
- **P1 §:** 8. **Depends on:** P9, P2. **Work:** short-term/session (Redis), long-term (`memories`), system (`system_settings`, versioned), trading (read path over signals/outcomes), knowledge (existing); user-scoped delete/export endpoints; retention.

### P11 — WebSocket
- **P1 §:** 3 (WSS), 9 (conversations/messages). **Depends on:** P9, P10, P1.
- **Work:** authenticated connection lifecycle, per-conversation channel isolation (builds on the P1 `conversations` ownership), message handling + `messages` persistence, Redis pub/sub fan-out, reconnect/error semantics.

### P12 — Remaining endpoint categories
- **P1 §:** 10. **P4:** G13. **Depends on:** P2, P10.
- **Work:** Users (admin CRUD), Trading Signals, History (save/list), Usage, Monitoring, Errors, Admin (settings with versioning, audit log read), feedback; analyze array-shape decision (D4). Backtesting/Paper-trading/Voice stay deferred (see §1).

### P13 — Admin Panel increment B
- **Depends on:** P12, P8, P10. **Work:** users, tools, knowledge, memory, usage, errors, system settings, monitoring, custom indicators.

### P14 — Documentation artifacts
- **P4:** G14 (folder rationale + concern map, **documenting the actual layout** and where it deliberately differs from the blueprint tree), G15 (roadmap rationale), G16 (10 source URLs), G01a (capability table), G07 (DB reasoning). OpenAPI export; refresh root/infra READMEs; guard or retire the `apply_*.sh` scripts. No runtime change.

### P15 — Final forensic reconciliation
- Requirement × Source × Implemented × Integrated × Tested × Evidence table for every P1 requirement, G01–G16, and master constraint; re-verify G04/G06/G09/G10/G11; classify anything blocked by credentials as *code complete / integration path complete / live verification blocked*.

## 4. Defects found during analysis → where they are fixed

| # | Defect (all reproduced against the baseline) | Phase |
|---|---|---|
| 1 | Rotating X-Forwarded-For bypasses rate limits (7/7 passed a 3/min limit) | **P1 ✔** |
| 2 | Non-IP / `ip:port` X-Forwarded-For breaks login+registration | **P1 ✔** |
| 3 | `logout` with lowercase `bearer` leaves the token valid | **P1 ✔** |
| 4 | "Optional" auth returns 500 when the DB is down | **P1 ✔** |
| 5 | Any registered user can delete/poison shared knowledge | **P1 ✔** |
| 6 | Unconfigured provider trips the breaker → misleading `circuit_open` | P2 |
| 7 | Fallback-to-mock overwrites `mock-v1` display name with "Grok (xAI)" | P2 |
| 8 | Duplicate `technical_indicators` rows on every request | P2 |
| 9 | 20k-row CSV → one 180,000-word chunk | P9 |
| 10 | Negative stop-loss accepted as a valid setup | P2 |
| 11 | RAG ignores `model_used`; switching embedding provider corrupts retrieval | P9 |
| 12 | Unhandled-error responses lack CORS headers | **P1 ✔** |
| 13 | Access-log line loses the request id | **P1 ✔** |
| 14 | R:R effectively constant 2.0 | D6 (by design) |
| 15 | `normalize_confidence(1)` = 100 | D5 (owner call) |
| — | Timing side-channel on unknown-email login | **P1 ✔** |
| — | `sessions.token_hash` unindexed on the hot path | **P1 ✔** (0005) |
| — | Gemma hosted path has no timeout/retry | P4 |
| — | `close_pool()` never called; `get_pool` race | P2 |

## 5. Progress

| Phase | Status |
|---|---|
| P1 | **DONE** — delivered with this document |
| P2 – P15 | Pending (resume at **P2**) |
