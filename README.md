# Oxygen AI — minimal end-to-end slice

A small, real, working slice of the Oxygen AI blueprint. It exists to
prove the one idea that actually matters architecturally: **indicators
are computed deterministically, the AI layer only reasons over them, and
every provider sits behind one swappable interface.** It is not the full
production system — see "Status against the blueprint" below for exactly
what that means.

## Correction applied

The blueprint's own §4.3.1 rule: Gemma 4 is a *provider identity*;
Google's Gemini API is only the *hosted transport* that reaches it —
never a separate provider. `geminiProvider.ts` → `gemmaProvider.ts`, id
`"gemini"` → `"gemma"`, env var → `GOOGLE_AI_API_KEY`. The `@google/genai`
SDK call is unchanged — that's genuinely the only transport to hosted
Gemma 4. A repo-wide grep confirms zero remaining `gemini`/`Gemini`
naming outside of comments explaining this exact distinction.

## What's actually real here

- `lib/indicators.ts` — SMA, RSI(14), ATR(14): real formulas, unit-tested.
- `lib/verify.ts` — the verification stage. Entry/stop/targets computed
  from ATR here, never from the AI's text. A consistency check fails
  closed to `NO_VALID_SETUP` rather than shipping a broken setup.
- `lib/types.ts` — the `TradeAnalysis` schema (Zod): schema-validated
  output, source-tagging (`mock` / `hosted_api` / `local_model`).
- `lib/providers/` — the `AIProvider` interface and four implementations:
  `mockProvider.ts` (offline, no key needed), `customAiProvider.ts`
  (any OpenAI-compatible endpoint — your choice of model, per blueprint
  §4.1), `gemmaProvider.ts` (Gemma 4 via `@google/genai`), and
  `grokProvider.ts` (Grok via a plain `fetch` to xAI).
- `infra/migrations/` — the full database schema (38 tables, plus a
  0002 fix — see below), applied and verified against a real PostgreSQL 16
  + pgvector instance. See `infra/migrations/README.md`.
- `lib/db/` — the route is now actually wired to that schema. When
  `DATABASE_URL` is set: instruments and OHLCV bars are read from and
  written to Postgres (persisted once per symbol, not regenerated every
  request), found signals are saved to `trading_signals`, and providers/
  models are upserted into `ai_providers`/`ai_models` on demand. Falls
  back to the old ephemeral synthetic-data path — cleanly, not a crash —
  if `DATABASE_URL` is unset, or set but unreachable.
- One route (`POST /api/analyze`) and one page running the whole pipeline
  end to end, with correct fallback-to-mock for all three real providers
  and correct fallback-to-ephemeral for the database.

## Status against the blueprint

Being precise about this rather than saying "mostly done" — this is
where the four passages' worth of specification actually stands right
now:

| Blueprint area | Status |
|---|---|
| §4 Custom AI / Grok / Gemma 4 as providers | **Real** — integration layer for all three, interface-uniform, type-checked, correct fallback. None live-tested (needs your keys). Custom AI's "orchestrating agent" / tool-calling half is not built. |
| §4.5 Provider Router — circuit breaker, health checks, model registry | **Not built.** Current fallback is one if-check, not the circuit-breaker-with-cooldown or persisted health-check history the blueprint specifies. |
| §4.6 Multi-provider comparison UI | **Not built.** UI picks one provider at a time. |
| §5 C++/CUDA performance layer | **Not built** — correctly so, per the blueprint's own §5.2/5.3: nothing has been profiled as a bottleneck, and MVP may stay Python by design. |
| §6 Deterministic trading engine | **Partial.** SMA/RSI/ATR only, of MACD/Stochastic/ADX/Bollinger/VWAP. No regime-awareness classifier, no market-structure/S-R detection. |
| §7 RAG & knowledge system | **Not built.** |
| §8 Memory architecture | **Not built** at the app level (schema exists; nothing reads/writes it yet). |
| §9 Database | **Schema real and verified, and now wired to the app.** `market_instruments`/`market_data`/`trading_signals`/`ai_providers`/`ai_models` are read/written for real when `DATABASE_URL` is set — verified against a live instance, not asserted (see below). Underlying bar data is still synthetic; `technical_indicators`, `memories`, RAG tables, and the rest of the 38 remain unused by the app. |
| §10 API architecture | **1 of ~7 endpoints** (`/api/analyze`, roughly `/api/ai/analyze`). |
| Passage 2 (admin panel, charting, voice, security, error center, backtesting, compliance) | **Not built** — this is nearly all of Passage 2. |
| Passage 3 (infra, CI, broader testing) | **Not built.** |

Doing all of the "not built" rows for real, in one pass, isn't something
a chat session can responsibly claim — it would mean generating
stub/untested code for a GPU-accelerated native layer, a live RAG
pipeline, voice infrastructure, and an admin panel, none of which I could
verify here, and reporting it as done. That's the thing this whole
project is supposed to avoid. What's above is real and checked; what
isn't is named plainly instead of faked.

## Running it

```bash
npm install
cp .env.example .env.local   # optional — add real keys to use a real provider
npm run dev
```

Open http://localhost:3000, type a symbol, hit Analyze.

## Verified before delivery (actually run, not claimed)

```
$ npm install                                            → exit 0
$ npx tsc --noEmit                                        → clean
$ npx tsx --test lib/indicators.test.ts lib/providers/index.test.ts
                                                            → 14/14 pass (3 consecutive clean runs;
                                                              one earlier run right after a fresh
                                                              install showed 1 flaky failure, not
                                                              reproduced since — noting it rather
                                                              than hiding it)
$ npx next build                                           → compiled successfully
$ npx next start, then POST /api/analyze for each provider:
    mock   → 200, SETUP_FOUND, real indicator-driven result
    custom → 200, correctly fell back to source: mock (no CUSTOM_AI_* set)
    grok   → 200, correctly fell back to source: mock (no XAI_API_KEY set)
    gemma  → 200, correctly fell back to source: mock (no GOOGLE_AI_API_KEY set)

Database wiring, verified against a real PostgreSQL 16 + pgvector instance
(not just written — checked with direct SQL, not inferred from the API
response):
    2x POST /api/analyze {symbol: HDFCBANK} with DATABASE_URL set
      → both 200, persisted: true, identical entry price both times
      → market_data row count for HDFCBANK: 60 (not 120 — the 2nd call
        read existing bars, it did not regenerate and re-insert)
      → market_instruments: 1 row · ai_providers: 1 row · ai_models: 1 row
        (the 0002 unique-constraint fix confirmed working — no duplicates
        despite two separate seed-model calls)
      → trading_signals: 2 rows (one per SETUP_FOUND call, as intended —
        each analysis is its own signal event, not deduplicated)
    POST /api/analyze with DATABASE_URL unset
      → 200, persisted: false, unchanged old behavior — confirms the
        no-DB path still works exactly as before this change
    POST /api/analyze with DATABASE_URL pointed at a closed port
      → 200, persisted: false — server log shows the caught
        ECONNREFUSED and the fall-through, not a 500
  POST /api/analyze {}                                     → 400 (validation works)
$ grep -rln "gemini" **/*.ts **/*.tsx                       → only explanatory comments/test names
$ database: see infra/migrations/README.md for the full log
```

Not verified: a live call to any of the three real providers — blocked by
this sandbox's network policy (locked to package registries), not by
anything in the code.

## If you want to keep going

Roughly in priority order: (1) get one real provider live-tested with a
real key, (2) swap the synthetic bar generator for a real market-data
vendor now that persistence exists to actually store what it returns,
(3) build the multi-provider comparison UI once two providers are
confirmed live, (4) fill out the indicator list, (5) everything else in
the status table above.
