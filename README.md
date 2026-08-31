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
