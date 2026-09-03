#!/usr/bin/env bash
# Completes Passage 4 §3.6: User rating + Select preferred result.
# Run from the repo root: bash apply_rating_feature.sh
set -euo pipefail

echo "Writing files..."
echo '  README.md'
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
| §4.6 Multi-provider comparison | **Real and now complete against §3.6.** Isolation via `asyncio.gather(..., return_exceptions=True)` (Passage 1 §4.5). 5 of 8 scoring axes computed for real; 3 render as the blueprint's own specified "not enough data yet" placeholder. Persists to the corrected `ai_comparisons`/`ai_comparison_results` schema. UI has all 6 §3.6 footer actions: Copy, Key evidence, Market data used, Data freshness, active-model badge (display-only, done previous pass); User rating and Select preferred (**this pass** — `PATCH /api/ai/comparisons/{id}`, backed by a new `user_rating` column, the existing `user_choice_provider_id`). Only "Why they disagree" remains, correctly deferred — Post-MVP per the blueprint's own classification. |
| §5 C++/CUDA performance layer | **Not built** — correctly so, per §5.2/5.3: nothing has been profiled as a bottleneck, and MVP may stay in the higher-level language by design. |
| §6 Deterministic trading engine | **Partial**, now genuinely Python + the specified baseline. SMA/RSI/ATR only, of MACD/Stochastic/ADX/Bollinger/VWAP. No regime-awareness classifier, no market-structure/S-R detection. |
| §7 RAG & knowledge system | **Not built.** |
| §8 Memory architecture | **Not built** at the app level (schema exists; nothing reads/writes it). |
| §9 Database | **Schema real and verified, wired to the app** — from Python via `asyncpg`. 3 migrations applied (0001 core schema, 0002 the ai_models unique-constraint fix, 0003 the user_rating column). |
| §10 API architecture | `POST /api/ai/analyze` (single + multi mode) and `PATCH /api/ai/comparisons/{id}` (rating + preference, new this pass). 2 of ~7 named endpoints. |
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

**Frontend §3.6 completion pass — Key evidence, Market data used, Data
freshness, active-model badge, Copy buttons:**
```
Backend additions this pass: dataTimestamp/isStale/timeframe on
TradeAnalysis, comparisonId on ComparisonResponse — mypy clean, 32/32
pytest pass (4 new tests, including a regression test for a real bug
found below).

Bug found and fixed: multi-provider slots hardcoded persisted=False
regardless of whether the request was actually DB-backed — caught while
threading the new freshness fields through comparison.py, confirmed live
(mock slot showed persisted:false even with DATABASE_URL set, before the
fix) and with a new regression test.

Second bug found and fixed: a fresh synthetic-data generation read as
"stale" immediately. Root cause: the generator's last bar is always
dated ~1 day before generation time by construction, and the freshness
threshold was exactly 1 day, so a few microseconds of processing time
pushed it over on every single call. Fixed by setting the threshold to 2
days (correct for daily bars, where "yesterday" is the normal freshest
value, not staleness) — confirmed live before and after the fix, not
just in the unit test.

$ mypy app --ignore-missing-imports        → clean (after both fixes)
$ pytest tests/ -q                          → 32/32 pass
$ npx tsc --noEmit                          → clean
$ npx next build                            → compiled successfully
$ Live check, single mode: response has all 17 fields the new JSX
  reads, including dataTimestamp/isStale/timeframe — confirmed via raw
  JSON, not just type-checked in isolation
$ Live check, compare mode: comparisonId is a real UUID (previously
  computed but never returned); "ok" slot carries model/
  supportingEvidence/dataTimestamp/isStale/timeframe; "unavailable" slot
  is exactly {outcome, providerId, reason}
$ Manually traced formatAnalysisAsText (the Copy button's output)
  against real captured API data, line by line — output is well-formed
```

Not verified this pass, same limitation as before: actual pixel-rendered
output in a browser — no browser is available in this sandbox. Verified
instead: the type contract end-to-end (`tsc` ties the JSX directly to
the response shape), the real API response contains every field the JSX
reads, and a manual trace of the formatting logic against real captured
data. That's a different, narrower claim than "confirmed visually," and
is reported as such rather than blurred together.

Not verified, unchanged: a live call to any of the three real providers
— blocked by this sandbox's network policy, not by anything in the code.

## §3.6 completed — User rating and Select preferred

```
Migration: infra/migrations/0003_add_comparison_rating.sql — adds
ai_comparisons.user_rating (smallint, 1-5). Applied and verified against
a live Postgres, same as 0001/0002.

New endpoint: PATCH /api/ai/comparisons/{id} — accepts rating and/or
preferredProviderId, at least one required. Both write to the same
ai_comparisons row via COALESCE (one field updateable without disturbing
the other) — verified live, not just via the unit test: rated a real
comparison 5/5, then sent a separate call setting only the preference,
confirmed the rating survived via a fresh GET-equivalent read.

Real design bug caught and fixed before it shipped: the initial
implementation would have returned userChoiceProviderId as the raw
internal ai_providers.id (a UUID) -- every other providerId in this API
is a name string ("grok", "mock"). Fixed by joining to ai_providers in
the read path; UPDATE...RETURNING can't join, so the update re-fetches
through the same enriched read afterward.

Validation: selecting a provider that wasn't part of the comparison
correctly 400s (checked against provider_set, not just "does this
provider exist anywhere"); an unknown comparisonId 404s; an empty body
400s; no DATABASE_URL configured 503s cleanly rather than crashing --
all four confirmed live, not just asserted.

CORS confirmed specifically for PATCH (a different method than the
GET/POST already checked) -- preflight response lists PATCH among
access-control-allow-methods, and the actual PATCH response carries the
same origin header.

$ mypy app --ignore-missing-imports        → clean
$ pytest tests/ -q                          → 38/38 pass (6 new)
$ npx tsc --noEmit                          → clean
$ npx next build                            → compiled successfully
$ Full live loop: create comparison → rate 5/5 → prefer "grok" → confirmed
  via direct SQL: user_rating=5, preferred=grok, provider_set=["mock","grok"]
```

## If you want to keep going

Roughly in priority order: (1) get one real provider live-tested with a
real key, (2) swap the synthetic bar generator for a real market-data
vendor, (3) fill out the indicator list, (4) circuit breaker + persisted
health checks for the provider router, (5) a new frontend surface (Admin
Panel is the natural first one — Passage 4 §8's own roadmap rationale
sequences it early, ahead of things that need it to be testable
against), (6) everything else in the status table above. §3.6 is now
fully implemented — Passage 4's comparison-UI recovery has nothing
outstanding except the explicitly-deferred "Why they disagree."
OXYGEN_AI_FILE_EOF

echo '  app/page.tsx'
cat > app/page.tsx << 'OXYGEN_AI_FILE_EOF'
"use client";

import { useState, type FormEvent } from "react";
import type { ComparisonFeedback, ComparisonResponse, ComparisonSlot, TradeAnalysis } from "@/lib/types";

const COMPARABLE_PROVIDERS = [
  { id: "mock", label: "Mock" },
  { id: "custom", label: "Oxygen AI" },
  { id: "grok", label: "Grok" },
  { id: "gemma", label: "Gemma 4" },
];

// Passage 4 §3.6's "Copy result — copies the rendered TradeAnalysis as
// text/markdown." One shared formatter so single- and compare-mode Copy
// buttons produce the same shape.
function formatAnalysisAsText(symbol: string, providerLabel: string, a: TradeAnalysis): string {
  const lines = [
    `${symbol} — ${providerLabel}`,
    a.status === "NO_VALID_SETUP" ? "No valid setup" : `${a.direction} @ ${a.entry}`,
  ];
  if (a.status === "SETUP_FOUND") {
    lines.push(`Stop: ${a.stopLoss}  Targets: ${a.targets.join(", ")}`);
    lines.push(`Confidence: ${a.confidence ?? "n/a"}  R:R: ${a.riskReward ?? "n/a"}`);
  }
  lines.push("", a.reasoningSummary);
  if (a.supportingEvidence.length > 0) {
    lines.push("", "Evidence:", ...a.supportingEvidence.map((e) => `- ${e}`));
  }
  lines.push(
    "",
    `source: ${a.source} · provider: ${a.provider} · model: ${a.model}`,
    `data: ${a.timeframe} as of ${a.dataTimestamp.slice(0, 10)} (${a.isStale ? "stale" : "live"})`
  );
  return lines.join("\n");
}

function CopyButton({ getText }: { getText: () => string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(getText());
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          // Clipboard access can be denied by the browser — fails
          // silently rather than breaking the rest of the page.
        }
      }}
      className="rounded border border-[#232830] px-2 py-1 font-mono text-[10px] uppercase tracking-wide text-[#7C8591] transition hover:border-[#4FD1C5] hover:text-[#4FD1C5]"
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

export default function Home() {
  const [symbol, setSymbol] = useState("");
  const [mode, setMode] = useState<"single" | "compare">("single");
  const [providerId, setProviderId] = useState("mock");
  const [compareIds, setCompareIds] = useState<string[]>(["mock", "grok"]);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<TradeAnalysis | null>(null);
  const [comparison, setComparison] = useState<ComparisonResponse | null>(null);
  const [feedback, setFeedback] = useState<ComparisonFeedback | null>(null);
  const [feedbackBusy, setFeedbackBusy] = useState(false);
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
    setFeedback(null);
    try {
      const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
      const body =
        mode === "single"
          ? { symbol: symbol.trim(), provider: providerId }
          : { symbol: symbol.trim(), providers: compareIds };
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

  async function submitFeedback(rating?: number, preferredProviderId?: string) {
    if (!comparison?.comparisonId) return;
    setFeedbackBusy(true);
    try {
      const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
      const res = await fetch(`${apiBase}/api/ai/comparisons/${comparison.comparisonId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rating, preferredProviderId }),
      });
      const data = await res.json();
      if (res.ok) setFeedback(data);
      // A failed rating/preference doesn't disturb the comparison
      // already on screen — it's a secondary action, not resubmitted.
    } catch {
      // Same reasoning — network hiccup on a footer action shouldn't
      // surface as a page-level error.
    } finally {
      setFeedbackBusy(false);
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

        {result && <ResultCard symbol={symbol.trim().toUpperCase()} result={result} />}
        {comparison && (
          <ComparisonGrid
            data={comparison}
            feedback={feedback}
            feedbackBusy={feedbackBusy}
            onRate={(rating) => submitFeedback(rating, undefined)}
            onPrefer={(providerId) => submitFeedback(undefined, providerId)}
          />
        )}
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

function ResultCard({ symbol, result }: { symbol: string; result: TradeAnalysis }) {
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
        <div className="flex items-center gap-3">
          {result.confidence !== null && (
            <span className="font-mono text-sm text-[#7C8591]">
              confidence <span className="text-[#E8EAED]">{result.confidence}</span>
            </span>
          )}
          <CopyButton getText={() => formatAnalysisAsText(symbol, result.provider, result)} />
        </div>
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

      <MarketDataStrip symbol={symbol} analysis={result} />

      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 border-t border-[#232830] pt-3 font-mono text-[11px] text-[#5B6470]">
        <span>source: {result.source}</span>
        <span>provider: {result.provider}</span>
        <span>model: {result.model}</span>
        <span>rr: {result.riskReward ?? "n/a"}</span>
        <span>persisted: {result.persisted ? "yes" : "no"}</span>
      </div>
    </div>
  );
}

// Passage 4 §3.6's "Market data used" (instrument, timeframe, price,
// data_timestamp) and "Data freshness" (data_timestamp + stale/live
// flag) fields, combined into one strip since they share the same
// underlying data.
function MarketDataStrip({ symbol, analysis }: { symbol: string; analysis: TradeAnalysis }) {
  return (
    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-[#5B6470]">
      <span>
        data: {symbol} · {analysis.timeframe} · {analysis.indicatorsUsed.lastClose}
      </span>
      <span className={analysis.isStale ? "text-[#FBBF24]" : "text-[#5B6470]"}>
        as of {analysis.dataTimestamp.slice(0, 10)} ({analysis.isStale ? "stale" : "live"})
      </span>
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

function ComparisonGrid({
  data,
  feedback,
  feedbackBusy,
  onRate,
  onPrefer,
}: {
  data: ComparisonResponse;
  feedback: ComparisonFeedback | null;
  feedbackBusy: boolean;
  onRate: (rating: number) => void;
  onPrefer: (providerId: string) => void;
}) {
  // The two mutating footer actions (Passage 4 §3.6) need a real,
  // persisted ai_comparisons row to attach to — there's nothing to PATCH
  // if this request ran on ephemeral synthetic data.
  const feedbackAvailable = data.persisted && Boolean(data.comparisonId);

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
          <ComparisonColumn
            key={slot.providerId}
            symbol={data.symbol}
            slot={slot}
            preferred={feedback?.userChoiceProviderId === slot.providerId}
            canPrefer={feedbackAvailable && !feedbackBusy}
            onPrefer={() => onPrefer(slot.providerId)}
          />
        ))}
      </div>

      {feedbackAvailable && (
        <div className="mt-3 flex items-center gap-2 rounded-lg border border-[#232830] bg-[#12151B] px-4 py-3">
          <span className="font-mono text-[11px] text-[#5B6470]">rate this comparison</span>
          <div className="flex gap-1">
            {[1, 2, 3, 4, 5].map((n) => (
              <button
                key={n}
                type="button"
                disabled={feedbackBusy}
                onClick={() => onRate(n)}
                className={`font-mono text-sm transition disabled:opacity-40 ${
                  feedback?.userRating && n <= feedback.userRating
                    ? "text-[#FBBF24]"
                    : "text-[#5B6470] hover:text-[#FBBF24]"
                }`}
              >
                ★
              </button>
            ))}
          </div>
          {feedback?.userRating && (
            <span className="font-mono text-[11px] text-[#5B6470]">{feedback.userRating}/5 saved</span>
          )}
        </div>
      )}
    </div>
  );
}

function ComparisonColumn({
  symbol,
  slot,
  preferred,
  canPrefer,
  onPrefer,
}: {
  symbol: string;
  slot: ComparisonSlot;
  preferred: boolean;
  canPrefer: boolean;
  onPrefer: () => void;
}) {
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
    <div
      className="rounded-lg border bg-[#12151B] p-4 transition"
      style={{ borderColor: preferred ? "#4FD1C5" : "#232830" }}
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="font-mono text-xs uppercase tracking-widest text-[#4FD1C5]">{slot.providerId}</p>
          {/* Active-model badge (Passage 4 §3.6) — which model actually
              answered, not just which provider, since an admin-changed
              default shouldn't leave users guessing. */}
          <p className="font-mono text-[10px] text-[#5B6470]">{analysis.model}</p>
        </div>
        <CopyButton getText={() => formatAnalysisAsText(symbol, slot.providerId, analysis)} />
      </div>

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

      {/* Key evidence (Passage 4 §3.6) — distinct from the prose
          summary above; was already sent by the API, just wasn't
          rendered in this view. */}
      {analysis.supportingEvidence.length > 0 && (
        <ul className="mt-2 space-y-0.5 font-mono text-[11px] text-[#7C8591]">
          {analysis.supportingEvidence.map((ev, i) => (
            <li key={i}>· {ev}</li>
          ))}
        </ul>
      )}

      <MarketDataStrip symbol={symbol} analysis={analysis} />

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

      {canPrefer && (
        <button
          type="button"
          onClick={onPrefer}
          disabled={preferred}
          className={`mt-3 w-full rounded border px-2 py-1.5 font-mono text-[11px] uppercase tracking-wide transition disabled:cursor-default ${
            preferred
              ? "border-[#4FD1C5] bg-[#4FD1C5]/10 text-[#4FD1C5]"
              : "border-[#232830] text-[#7C8591] hover:border-[#4FD1C5] hover:text-[#4FD1C5]"
          }`}
        >
          {preferred ? "Preferred" : "Prefer this"}
        </button>
      )}
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

echo '  apps/api/app/db/comparisons.py'
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


async def get_comparison(comparison_id: str) -> dict | None:
    pool = await get_pool()
    # LEFT JOIN so userChoiceProviderId comes back as the same name
    # string ("grok", "mock", ...) every other providerId in this API
    # uses — not the internal ai_providers.id UUID the column actually
    # stores.
    row = await pool.fetchrow(
        """
        SELECT ac.id, ac.provider_set, ap.name AS user_choice_provider_name, ac.user_rating
        FROM ai_comparisons ac
        LEFT JOIN ai_providers ap ON ap.id = ac.user_choice_provider_id
        WHERE ac.id = $1
        """,
        comparison_id,
    )
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "providerSet": json.loads(row["provider_set"]),
        "userChoiceProviderId": row["user_choice_provider_name"],
        "userRating": row["user_rating"],
    }


async def get_provider_db_id_by_name(name: str) -> str | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT id FROM ai_providers WHERE name = $1", name)
    return str(row["id"]) if row else None


async def update_comparison_feedback(
    comparison_id: str, rating: int | None, preferred_provider_db_id: str | None
) -> dict | None:
    """
    Partial update — COALESCE leaves a column unchanged when its
    parameter is None, so a request that only sets one of the two
    (rating or preference) doesn't clobber the other. Re-fetches through
    get_comparison() afterward so the response carries the provider
    *name*, not the raw ai_providers.id the column stores (UPDATE's
    RETURNING can't join another table).
    """
    pool = await get_pool()
    updated = await pool.fetchrow(
        """
        UPDATE ai_comparisons
        SET user_rating = COALESCE($2, user_rating),
            user_choice_provider_id = COALESCE($3, user_choice_provider_id)
        WHERE id = $1
        RETURNING id
        """,
        comparison_id,
        rating,
        preferred_provider_db_id,
    )
    if updated is None:
        return None
    return await get_comparison(comparison_id)
OXYGEN_AI_FILE_EOF

echo '  apps/api/app/main.py'
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

from .routers import analyze, comparisons

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
app.include_router(comparisons.router)
OXYGEN_AI_FILE_EOF

echo '  apps/api/app/routers/comparisons.py'
cat > apps/api/app/routers/comparisons.py << 'OXYGEN_AI_FILE_EOF'
"""
Passage 4 §3.6's two mutating comparison footer actions: User rating and
Select preferred result. Both write to the ai_comparisons row the
original POST /api/ai/analyze (multi-provider mode) already created —
this is why that endpoint now returns comparisonId.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..db.client import is_db_configured
from ..db.comparisons import get_comparison, get_provider_db_id_by_name, update_comparison_feedback
from ..schemas import ComparisonFeedback, RateComparisonRequest

router = APIRouter()


@router.patch("/api/ai/comparisons/{comparison_id}")
async def rate_comparison(comparison_id: str, body: RateComparisonRequest) -> ComparisonFeedback:
    if not is_db_configured():
        raise HTTPException(
            status_code=503, detail="Ratings require a database — DATABASE_URL is not set"
        )
    if body.rating is None and body.preferredProviderId is None:
        raise HTTPException(status_code=400, detail="Provide rating and/or preferredProviderId")

    existing = await get_comparison(comparison_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="No comparison found with that id")

    preferred_db_id = None
    if body.preferredProviderId is not None:
        # You can only prefer a provider that was actually part of this
        # comparison — not any provider id that happens to exist.
        if body.preferredProviderId not in existing["providerSet"]:
            raise HTTPException(
                status_code=400,
                detail=f"'{body.preferredProviderId}' was not part of this comparison "
                f"(participants: {', '.join(existing['providerSet'])})",
            )
        preferred_db_id = await get_provider_db_id_by_name(body.preferredProviderId)
        if preferred_db_id is None:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {body.preferredProviderId}")

    updated = await update_comparison_feedback(comparison_id, body.rating, preferred_db_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="No comparison found with that id")

    return ComparisonFeedback(
        comparisonId=updated["id"],
        providerSet=updated["providerSet"],
        userChoiceProviderId=updated["userChoiceProviderId"],
        userRating=updated["userRating"],
    )
OXYGEN_AI_FILE_EOF

echo '  apps/api/app/schemas.py'
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
    # Passage 4 §3.6's "Data freshness" field: the underlying market
    # data's own timestamp (the last bar used), plus a stale/live flag —
    # distinct from generatedAt, which is when the analysis was computed,
    # not when the price data itself is as-of.
    dataTimestamp: str
    isStale: bool
    # Passage 4 §3.6's "Market data used" field also names timeframe —
    # a fixed literal for now since "1d" is the only timeframe this
    # system computes anywhere; typed this way (not a bare str) so
    # adding a second timeframe later is a type-checked change, not a
    # silent one.
    timeframe: Literal["1d"] = "1d"


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
    # The ai_comparisons row's id, when persisted — the frontend needs
    # this to submit a rating or a preferred-provider choice against the
    # comparison later (Passage 4 §3.6's remaining footer actions; not
    # implemented this pass, but the response needs to carry the id now
    # so that step doesn't require touching this contract again).
    comparisonId: Optional[str] = None


class AnalyzeRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    provider: Optional[str] = None
    providers: Optional[list[str]] = Field(default=None, min_length=2, max_length=3)


class RateComparisonRequest(BaseModel):
    """
    Passage 4 §3.6's two mutating footer actions, on one endpoint since
    both write to the same ai_comparisons row: User rating (1-5, shared
    across the whole comparison) and Select preferred result (per
    column — the id of whichever provider the user picked). Either can
    be sent alone; at least one must be present.
    """

    rating: Optional[int] = Field(default=None, ge=1, le=5)
    preferredProviderId: Optional[str] = None


class ComparisonFeedback(BaseModel):
    comparisonId: str
    providerSet: list[str]
    userChoiceProviderId: Optional[str]
    userRating: Optional[int]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
OXYGEN_AI_FILE_EOF

echo '  apps/api/tests/test_rate_comparison_schema.py'
cat > apps/api/tests/test_rate_comparison_schema.py << 'OXYGEN_AI_FILE_EOF'
import pytest
from pydantic import ValidationError

from app.schemas import RateComparisonRequest


def test_accepts_rating_alone():
    req = RateComparisonRequest(rating=4)
    assert req.rating == 4
    assert req.preferredProviderId is None


def test_accepts_preferred_provider_alone():
    req = RateComparisonRequest(preferredProviderId="grok")
    assert req.preferredProviderId == "grok"
    assert req.rating is None


def test_accepts_both_together():
    req = RateComparisonRequest(rating=5, preferredProviderId="gemma")
    assert req.rating == 5
    assert req.preferredProviderId == "gemma"


def test_rejects_rating_above_5():
    with pytest.raises(ValidationError):
        RateComparisonRequest(rating=6)


def test_rejects_rating_below_1():
    with pytest.raises(ValidationError):
        RateComparisonRequest(rating=0)


def test_accepts_neither_at_the_schema_level():
    # The "at least one required" rule is enforced by the router, not
    # the schema -- both fields are legitimately optional individually.
    req = RateComparisonRequest()
    assert req.rating is None
    assert req.preferredProviderId is None
OXYGEN_AI_FILE_EOF

echo '  infra/migrations/0003_add_comparison_rating.sql'
cat > infra/migrations/0003_add_comparison_rating.sql << 'OXYGEN_AI_FILE_EOF'
-- Adds the one column ai_comparisons was missing for Passage 4 §3.6's
-- "User rating" footer action. user_choice_provider_id (Select
-- preferred) already existed from 0001 — this is the other half.

ALTER TABLE ai_comparisons
  ADD COLUMN user_rating smallint CHECK (user_rating BETWEEN 1 AND 5);
OXYGEN_AI_FILE_EOF

echo '  lib/types.ts'
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
  dataTimestamp: string;
  isStale: boolean;
  timeframe: "1d";
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
  comparisonId: string | null;
}

// Passage 4 §3.6's mutating footer actions — User rating and Select
// preferred result — both PATCH /api/ai/comparisons/{id}.
export interface ComparisonFeedback {
  comparisonId: string;
  providerSet: string[];
  userChoiceProviderId: string | null;
  userRating: number | null;
}
OXYGEN_AI_FILE_EOF

echo ""
echo "Done. Next steps:"
echo "  psql -f infra/migrations/0003_add_comparison_rating.sql <your-db>"
echo "  cd apps/api && .venv/bin/mypy app --ignore-missing-imports"
echo "  .venv/bin/pytest tests/ -v"
echo "  cd .. && npx tsc --noEmit && npm run build"