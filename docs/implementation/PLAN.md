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
| Models CRUD / named endpoints | **Built (P3)**: models CRUD, `/compare`, provider settings, breaker admin | remaining named categories → **P12** |

### Passage 1 section status

| P1 § | Requirement | Baseline | Phase |
|---|---|---|---|
| §3 | REST gateway, auth, rate-limit | Built; auth/rate-limit hardened in P1 | P1 ✔ |
| §3 | WebSocket | Missing | P11 |
| §3 | Redis (cache/queue) | Missing | P9 |
| §3 | Market Data Abstraction | Missing | P7 |
| §4.1 | Custom AI as orchestrating agent | Provider only; `agent/orchestrator.py` referenced in docstrings but absent | P6 |
| §4.1 / G01b | Admin-authored custom indicators (constrained expression tree) | Missing | P8 |
| §4.2 / G02 | Grok slugs, redirects, Responses/Agent-Tools API, registry automation | Slug handling built; **DB flip + persisted alert ✔ (P3)** — runtime catch + periodic poll (poll mock-tested only); Responses API absent | P6 |
| §4.3 | Gemma 4 identity vs transport | Built | — |
| §4.4 / §4.4.1 | Offline runtimes, admin fields, controlled fallback, `local_model` tag | Transport only | P4 |
| §4.5 / G03 / G11 | Router, breaker (5/60s, **admin-configurable**), fallback, `ai_health_checks` persisted | Built; **configurability ✔ (P3, `system_settings`, per-provider, live)**; persistence ✔ (P2); health stage + fallback model ✔ (P3) | — |
| §4.6 / G04 / G09 / G10 | Comparison, scoring axes, placeholders, footer actions, model badge | Built; "Save → History" missing; "Why they disagree" is Post-MVP (P3 §27) | P12/P13 |
| §4.7 | Reasoning pipeline | Built | — |
| §4.8 / G05 | Tool registry governed, provider-agnostic | Registry built; not wired to providers | P6 |
| §5 | C++/CUDA layer, pybind11, parity gate, pure-Python fallback | Built and verified | — |
| §6 / G06 | Deterministic engine | Built | — |
| §7 | RAG pipeline + source tags | Built; hardening | P9 |
| §8 | Six memory layers + delete/export | 1 of 6 | P10 |
| §9 / G12 | Schema, relationships, indexing, retention | Schema complete (0001–0005); operational usage partial | P2, P10, P12 |
| §10 / G08 / G13 | Named endpoints, 19 categories, contract shape | 33 routes (18 + 15 in P3) of the required set | P12 (see ENDPOINT_CHECKLIST.md) |

### Passage 4 G01–G16

| ID | Status in baseline | Action | Phase |
|---|---|---|---|
| G01 | a: table is documentation; b: mechanism missing | Docs for (a); implement evaluator for (b) | P14 / P8 |
| G02 | Built except the Responses API path | Responses API path | P6 |
| G03 | **Done (P3)**: 5/60 s default, admin-configurable globally and per provider, applied live | — | — |
| G04, G10, G11 | Built | Re-verify in final audit | P15 |
| G05 | Registry built, governance partial | Roles/modes, orchestration | P6 |
| G06 | Built | Re-verify | P15 |
| G07 | Rationale only needs documenting | Docs | P14 |
| G08 | **Done (P3)**: `/route`, models CRUD (admin, audited), `/compare`, snake_case probe fields | — | — |
| G09 | Built except "Save → History" | History list + save | P12/P13 |
| G12 | Schema ✔; see table below | Per-table ownership | P2/P10/P12 |
| G13 | 33 of the required endpoints | Full reconciliation | P12 |
| G14, G15, G16 | Missing documentation | Docs only, no runtime change | P14 |

### G12 — table ownership (not "make every count non-zero")

| Table | Owner subsystem | Operational today? | Plan |
|---|---|---|---|
| users, roles, permissions, sessions | Auth | Yes | P1; expired-session purge **P2 ✔** |
| ai_providers, ai_models | Router/registry | **Yes (P3 ✔)** — the database is the registry's system of record: loaded at startup + every `RUNTIME_CONFIG_REFRESH_SECONDS` + on every admin write; catalogue seeded once (`system_settings.model_catalogue_seeded`) | P12/P13 read-side dashboards |
| ai_provider_settings | Provider admin | **Yes (P3 ✔)** — API key (encrypted), base URL, timeout, retries, region; admin override > environment | P4 adds the local-runtime fields (the table has no jsonb column: P4 decides between one in 0008 or `ai_models.config`) |
| ai_routing_rules, ai_fallback_rules | Router | **No — decided dormant in P3** (see P3 "Decisions") | revisit only if Passage 2's admin routing spec defines their semantics |
| ai_health_checks | Router monitoring | **Yes — written by provider probes (P2 ✔)** | P12/P13 read it |
| ai_comparisons, ai_comparison_results | Comparison | Yes | — |
| market_instruments, market_data, technical_indicators | Market data / engine | Yes. `technical_indicators`: unique key + upsert **P2 ✔** (migration 0006) | `market_data` unique key lands with the P7 adapters — on today's synthetic bars (timestamps anchored to *now*) a unique key could not prevent duplicates; real vendor bars have stable timestamps |
| trading_signals, feedback | Engine / evaluation | signals yes; feedback no | P12 |
| knowledge_*, embeddings | RAG | Yes | P9 |
| conversations | Conversation/WebSocket | **Started in P1** (comparison ownership) | P11 |
| messages | WebSocket | No | P11 |
| memories | Memory | No | P10 |
| tool_registry, tool_calls | Tools | Yes | P6 |
| custom_indicator_definitions | Custom indicators | No | P8 |
| error_logs, audit_logs, usage_logs | Ops | **Yes (P2 ✔)**: usage in single + multi mode; `error_logs` from the 500 handler; `audit_logs` from knowledge upload/delete, tool invoke, CLI role changes | P3 adds models-CRUD/provider-settings audit; P12 adds the read endpoints |
| system_settings | System memory | **Yes (P3 ✔)** — `circuit_breaker`, `model_catalogue_seeded`; history = the audit trail | P12 generic settings endpoints |
| feature_flags | Ops | No | Dormant until an owning feature exists |
| strategies, strategy_versions, backtests, trade_setups, trade_results | Backtesting / paper trading (P2 §18) | No | **Intentionally dormant** — owner subsystem not in this scope |
| voice_settings | Voice (P2) | No | **Intentionally dormant** |
| Retention | audit_logs/trade_results indefinite; sessions/memories expire; tick roll-up N/A (no tick data) | **Yes (P2 ✔)** — `db/retention.py`, in-process loop + `app.cli purge-expired` | — |

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

### P2 — Persistence hygiene & operational foundations — **DONE**
- **Objective:** fix the correctness/ops defects later phases build on; give G12's operational tables their writers.
- **P1 §:** 4.5 (health checks persisted), 9 (indexing, retention). **P4:** G12 (b,c,d), G03.
- **Depends on:** P1.
- **Work:** `technical_indicators` unique key + upsert (duplicate rows per request today); `audit_logs` writer; `error_logs` persistence from the 500 handler (error id ↔ row); `ai_health_checks` writes from `/health` probes; usage logging in multi mode; mock-vs-fallback `display_name` corruption; breaker must not count "not configured" as a failure; negative-stop guard (`stop>0`); `get_pool` race + `close_pool` on shutdown; retention job (expired sessions, expired memories).
- **Files:** `db/{indicators_db,audit,errors,health,usage,retention}.py`, `providers/{circuit_breaker,registry}.py` (minimal), `engine/risk_engine.py`/`verify.py` (guard), `main.py` lifespan.
- **DB:** `0006_*` (unique index on technical_indicators with dedupe of existing rows; partial index `error_logs(status) WHERE status!='CLOSED'`). **API:** none. **Frontend:** none.
- **Tests:** DB integration for each writer; breaker config-error regression; retention. **Verify:** full suite + mypy. **Complete when:** every item has a failing-before/passing-after test.
- **Delivered (deviations from the plan above, with reasons):**
  - *No partial index on `error_logs`* — `idx_error_logs_open` already exists from 0001 (and uses the lowercase `'closed'` the CHECK constraint requires, not the spec's `'CLOSED'`).
  - *`market_data` unique key deferred to P7* — see the G12 table.
  - *Breaker fix lives in `providers/{base,registry}.py`* (`ProviderNotConfiguredError`, checked before the breaker is touched), not in `circuit_breaker.py`, which is unchanged.
  - *Also fixed (negative-price family):* a SHORT's far targets at or below zero are dropped, and the setup is rejected if none remain — same root cause as the negative stop, found while writing its test.
  - *New files:* `db/{audit,errors,health,retention}.py`, `infra/migrations/0006_persistence_hygiene.sql`, `tests/test_p2_{foundations,persistence}.py`. *Modified:* `db/{client,signals,comparisons,indicators_db}.py`, `providers/{base,registry}.py`, `comparison.py`, `engine/risk_engine.py`, `errors.py`, `main.py`, `cli.py`, `config.py`, `routers/{analyze,providers,knowledge,tools}.py`, three test files whose cleanup now has to clear the (RESTRICT) audit trail, one test that pinned the overwrite-on-conflict display-name defect, READMEs/env example/checklist.
  - *Every fix was mutation-checked:* reverting it individually makes a named test fail (10/10).

### P3 — Provider settings, model registry persistence, Models CRUD, named endpoints — **DONE**
- **Objective:** DB becomes the registry's system of record; admins manage models/providers through the API.
- **P1 §:** 4.2, 4.5, 9.1, 10. **P4:** G02g, G03 (admin-configurable breaker), G08a/b, G12.
- **Depends on:** P2 (audit writer).
- **Work:** registry loads/syncs `ai_models` + seven-value status enum; `GET/POST/PATCH/DELETE /api/ai/models` (admin, audited); `ai_provider_settings` read/write incl. encrypted API keys (env stays authoritative unless admin override set; master key via env, never invented); breaker thresholds from settings; retired-slug auto-flip + alert persisted; `POST /api/ai/compare` (2–3 providers); snake_case `latency_ms`/`model_checked` aliases on test/health; decide `ai_routing_rules`/`ai_fallback_rules`; router health-check stage + per-provider fallback model (P1 §4.5 pseudocode, defect 23); explicit "requested X, answered by mock" signal in single mode (defect 21); `CancelledError`-safe half-open trial (defect 22).
- **DB:** only if CHECK/enum or new column needed (decided after inspecting 0001) — **result: migration `0007`, no new table/column** (the 7-value status CHECK already existed in 0001). **Frontend:** none (P5).
- **Tests:** CRUD authz matrix, audit rows, encryption round-trip, registry reload, compare endpoint parity with analyze multi.
- **Delivered:** migration `0007_provider_registry.sql`; `security/secret_store.py`; `db/{models,provider_settings,system_settings}.py`; `providers/{provider_settings,model_health,lifecycle,runtime_config,availability}.py`; `routers/models.py`; registry/breaker/provider changes; `POST /api/ai/compare`; **15 new routes**; `cryptography==44.0.0`; CLI `generate-settings-key`; three new background jobs. 156 new tests (78 unit · 32 DB-layer · 46 HTTP); every fix mutation-checked (18/18).
- **Decisions (and why):**
  - *Model selection precedence:* an environment variable (`GROK_MODEL_ID`…) **still wins when set**; otherwise the registry's default. It is the same rule as for settings ("environment first, admin override on top"), keeps every `.env`-only deployment unchanged, and is shown to admins as `modelSelectionSource` on `GET /api/ai/providers` so a no-op default change is explained, not mysterious.
  - *Statuses:* `active|hosted|local` may be a provider's default; `fallback` only the fallback; `deprecated` is resolvable but never auto-selected; `disabled|retired` are never called. A `fallback` model that is made (or promoted to) the default becomes `active`, because the database forbids a default with status `fallback`.
  - *Retiring/deleting a default promotes the fallback* (and leaves the fallback slot empty) rather than leaving the provider with no model; a provider with no fallback is left without a default and reports `no_model`.
  - *Deleting a model with history is refused (409):* every FK to `ai_models` is `ON DELETE SET NULL`, so a delete would silently strip attribution from the evaluation dataset. Retire it instead. A test asserts the list of referencing tables against `pg_constraint`, so a future migration cannot add an FK unnoticed.
  - *Router health stage is remembered, not probed:* a per-request "ping" would double the calls and token cost of every hosted provider. A model is unhealthy after 2 consecutive failures or a failed admin probe (120 s TTL). **Deviation from P1 §4.5's pseudocode:** with no healthy fallback model the default is *still attempted* (the pseudocode returns "unavailable"); a remembered flag with no recovery probe would lock the model out, and the circuit breaker already provides the cooldown-and-trial mechanism. The substitution is reported in `validationWarnings`.
  - *Disabled provider / no usable model are states, like "not configured":* never counted by the breaker; `reasonCode` `disabled` / `no_model`.
  - *G02g, both halves:* the catch-a-4xx path is always on, with a deliberately conservative classifier (a 404 from a mistyped base URL, a 400 about another parameter, or a network error containing the word "model" must not retire anything — the old heuristic retired a model on *any* 400/404 mentioning "model"). The periodic poll retires only models the router *uses*, ignores an unreadable/empty list, and ignores a list that shares no model with the registry. Default 6 h, `MODEL_AVAILABILITY_INTERVAL_SECONDS=0` disables, and `POST /api/ai/models/availability-check` runs it now.
  - *`ai_routing_rules` / `ai_fallback_rules` stay dormant:* the router's mode→providers mapping is explicit in each request (P1 §4.5), neither Passage 1 nor 4 defines rule semantics, evaluation order or a UI for them, and cross-provider fallback would undermine a comparison column's identity. Their natural consumer is the Admin Panel's routing screen (Passage 2, not part of the supplied sources).
  - *`region` is stored but consumed by nothing* — it is reserved for the Vertex AI initialisation P1 §4.3.1 mentions; not wired to anything just to make the column look used. A Gemma `baseUrl` override applies to the **local runtime** URL (the hosted transport has none); Grok's must be `https` (the key is sent there).
- **Not live-verified (external dependency):** both vendors' model-list endpoints (`GET {xAI}/models`, the Google SDK `models.list`) and a real xAI/Google key were unavailable; the poll's decisions are tested against fake lists, the two list readers against an httpx mock transport / their no-credential paths. Code complete, integration path complete, live verification blocked.

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
- **Work:** Redis client + health; shared rate-limit counters; cache; session-memory store; pub/sub primitive for P11; (real Redis available via apt in the sandbox). RAG: filter by `model_used`, split oversize chunks (a 20k-row CSV currently becomes one 180k-word chunk), `RETRIEVAL_QUERY` task type, embedding batching, rerank decision, re-index `ivfflat` after data load (defect 17), size-check uploads before reading them into memory.
- **Already done by hotfix H1 (do not redo):** Unicode-aware tokenizer, zero/non-finite vector handling at ingest/query/SQL/rerank/schema, `app.rag.maintenance reembed-unusable`.

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
| 6 | Unconfigured provider trips the breaker → misleading `circuit_open` | **P2 ✔** |
| 7 | Fallback-to-mock overwrites `mock-v1` display name with "Grok (xAI)" (and the comparison path wrote the bare provider id — last writer won) | **P2 ✔** (never overwritten on conflict; 0006 repairs damaged rows) |
| 8 | Duplicate `technical_indicators` rows on every request (reproduced: 132 rows / 22 duplicate groups after 12 calls on 2 symbols; after 0006 + P2 code, still 22 rows after 12 more calls) | **P2 ✔** |
| 9 | 20k-row CSV → one 180,000-word chunk | P9 |
| 16 | **Knowledge search → HTTP 500 `Out of range float values are not JSON compliant: nan`** (all-zero embedding for text with no ASCII letters/digits → pgvector NaN similarity). Also silently put the degenerate chunk into the analysis prompt context and returned `score: null` from the knowledge tool | **Hotfix H1 ✔** |
| 18 | `validate_bars` accepted `high=+inf`, `low=-inf`, `volume`=NaN/+inf (4 of 15 non-finite combinations) — latent with demo data, would produce non-finite indicators/response fields with real vendor data | **Hotfix H1 ✔** |
| 17 | `ivfflat` index was created on an empty table (pgvector's own notice: "low recall"); under an index plan a 4-row corpus returns 0 rows with the old *and* new query — pre-existing, unrelated to H1 | P9 |
| 10 | Negative stop-loss accepted as a valid setup (also: SHORT targets ≤ 0) | **P2 ✔** |
| 11 | RAG ignores `model_used`; switching embedding provider corrupts retrieval | P9 |
| 12 | Unhandled-error responses lack CORS headers | **P1 ✔** |
| 13 | Access-log line loses the request id | **P1 ✔** |
| 14 | R:R effectively constant 2.0 | D6 (by design) |
| 15 | `normalize_confidence(1)` = 100 | D5 (owner call) |
| — | Timing side-channel on unknown-email login | **P1 ✔** |
| — | `sessions.token_hash` unindexed on the hot path | **P1 ✔** (0005) |
| — | Gemma hosted path has no timeout/retry | P4 |
| — | `close_pool()` never called; `get_pool` race | **P2 ✔** |
| 19 | Persisted bars are never refreshed: once 60 exist, `get_or_refresh_bars` returns them forever, so every symbol is permanently "stale" after 2 days; `compute_freshness`'s docstring assumes a refresh path that does not exist. *Found while validating P1/P4 against the code; not in the earlier plan.* | P7 |
| 20 | `lib/types.ts` omits what the API returns (regime, structure, risk, contradictingEvidence, validationWarnings, knowledgeSources, transport, `latencyMs`, `reasonCode`) and its `source` union lacks `"fallback"`/`"cache"`; the UI renders none of it | P5 |
| 21 | Single-provider mode silently substitutes mock for an unconfigured provider (only the `provider` field reveals it) | **P3 ✔** (`requestedProvider` + a warning naming the reason) |
| 22 | `execute_provider`: a cancelled request (`CancelledError`, e.g. client disconnect) during a half-open trial leaves `half_open_in_flight` set, locking the provider out | **P3 ✔** (`release_trial`) |
| 23 | P1 §4.5 router pseudocode steps not implemented: health-check stage and a per-provider **fallback model** (`registry.get_fallback`) before declaring unavailable | **P3 ✔** (see P3 "Decisions" for the one deliberate deviation) |
| 24 | `native/README.md` documents `-DOXYGEN_ENABLE_CUDA`; the CMake option is `OXYGEN_ENABLE_CUDA` (the documented flag silently does nothing) | P14 |
| 25 | Root `README.md` and `infra/migrations/README.md` are stale (RAG/breaker "Not built", "3 migrations"/"One migration") | P14 |
| 26 | `GrokProvider._maybe_mark_retired` flipped a model to `retired` on *any* 400/404 whose text contained "model" — e.g. a mistyped base URL would have retired a working model once the flip was persisted | **P3 ✔** (conservative classifier) |
| 27 | `ai_health_checks` and `error_logs` have no retention (P2's purge covers sessions/memories only); every admin probe appends a row | P12 (with the monitoring reads) |
| 28 | `GemmaProvider.list_available_models` (Google SDK `models.list`) and the SDK's exception shape are unverified against a real key | P4/P6 live check |

## 5. Progress

| Phase | Status |
|---|---|
| P1 | **DONE** — delivered with this document |
| H1 — knowledge-search NaN hotfix | **DONE** — requires P1; see `apps/api/app/rag/{tokenize,maintenance}.py` |
| P2 — persistence hygiene & operational foundations | **DONE** — requires P1 + H1; migration `0006` |
| P3 — provider settings, model registry persistence, Models CRUD, named endpoints | **DONE** — requires P2; migration `0007`, `cryptography` dependency |
| P4 – P15 | Pending (resume at **P4**) |
