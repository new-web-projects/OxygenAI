#!/usr/bin/env bash
# Completes Passage 4 §3.6's display requirements for the comparison UI.
# Run from the repo root: bash apply_frontend_completion.sh
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
| §4.6 Multi-provider comparison | **Real**, in Python using the blueprint's own literal mechanism — `asyncio.gather(..., return_exceptions=True)` (Passage 1 §4.5), not a JS equivalent of it. 5 of 8 scoring axes computed for real; 3 render as the blueprint's own specified "not enough data yet" placeholder. Persists to the corrected `ai_comparisons`/`ai_comparison_results` schema, `comparisonId` now correctly returned to the caller. UI shows Key evidence, Market data used, Data freshness, and an active-model badge per §3.6 (this pass). Still missing: the 2 mutating footer actions (User rating, Select preferred — each needs a new endpoint, next up) and "Why they disagree" (Post-MVP per the blueprint's own classification, not scheduled). |
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

## If you want to keep going

Roughly in priority order: (1) the 2 mutating comparison footer actions
— User rating and Select preferred — each needs one new small endpoint;
`ai_comparisons.user_choice_provider_id` already exists for the second
one, the first needs a small new column, (2) get one real provider
live-tested with a real key, (3) swap the synthetic bar generator for a
real market-data vendor, (4) fill out the indicator list, (5) circuit
breaker + persisted health checks for the provider router, (6) a new
frontend surface (Admin Panel is the natural first one — Passage 4 §8's
own roadmap rationale sequences it early, ahead of things that need it
to be testable against), (7) everything else in the status table above.
OXYGEN_AI_FILE_EOF

echo '  app/page.tsx'
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
          <ComparisonColumn key={slot.providerId} symbol={data.symbol} slot={slot} />
        ))}
      </div>
    </div>
  );
}

function ComparisonColumn({ symbol, slot }: { symbol: string; slot: ComparisonSlot }) {
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

echo '  apps/api/app/comparison.py'
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


async def _run_one(
    provider_id: str,
    symbol: str,
    indicators: IndicatorBundle,
    last_bars: list[dict],
    model_id: str,
    persisted: bool,
    data_timestamp: str,
    is_stale: bool,
) -> ComparisonSlotOk:
    provider = get_provider_strict(provider_id)
    reasoning = await provider.reason(
        AnalysisContext(symbol=symbol, indicators=indicators, last_bars=last_bars)
    )
    source = "mock" if provider_id == "mock" else "hosted_api"
    # persisted/data_timestamp/is_stale are properties of the shared
    # underlying bars every provider in this comparison reasons over —
    # not per-provider, so the same values apply to every slot.
    analysis = build_trade_analysis(
        indicators, reasoning, source, provider_id, model_id, persisted, data_timestamp, is_stale
    )
    return ComparisonSlotOk(providerId=provider_id, analysis=analysis, scores=score_analysis(analysis))


async def run_comparison(
    provider_ids: list[str],
    symbol: str,
    indicators: IndicatorBundle,
    last_bars: list[dict],
    resolve_model_id,
    persisted: bool,
    data_timestamp: str,
    is_stale: bool,
) -> list[ComparisonSlot]:
    settled = await asyncio.gather(
        *[
            _run_one(
                pid, symbol, indicators, last_bars, resolve_model_id(pid), persisted, data_timestamp, is_stale
            )
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

echo '  apps/api/app/routers/analyze.py'
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
from ..providers.base import AnalysisContext
from ..providers.registry import resolve_provider
from ..schemas import AnalyzeRequest, ComparisonResponse, TradeAnalysis, now_iso
from ..utils import compute_freshness
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

    # Data freshness (Passage 4 §3.6): a property of the underlying bars,
    # not of any one provider's reasoning — computed once, shared by
    # every slot in both modes below.
    data_timestamp = bars[-1].timestamp
    is_stale = compute_freshness(data_timestamp)

    # ---- Multi-provider comparison mode ----
    if body.providers:
        validation_error = validate_provider_set(body.providers)
        if validation_error:
            raise HTTPException(status_code=400, detail=validation_error)

        results = await run_comparison(
            body.providers, symbol, indicators, last_bars, resolve_model_id, persisted, data_timestamp, is_stale
        )

        comparison_id: str | None = None
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
                comparison_id = await save_comparison(
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
            comparisonId=comparison_id,
        )

    # ---- Single-provider mode (unchanged contract, plus freshness) ----
    provider = resolve_provider(body.provider)
    if provider.id == "mock":
        source = "mock"
    elif provider.id == "custom" and os.environ.get("CUSTOM_AI_SOURCE_TAG") == "local_model":
        source = "local_model"
    else:
        source = "hosted_api"
    model_id = resolve_model_id(provider.id)

    try:
        reasoning = await provider.reason(AnalysisContext(symbol=symbol, indicators=indicators, last_bars=last_bars))
        analysis: TradeAnalysis = build_trade_analysis(
            indicators, reasoning, source, provider.id, model_id, persisted, data_timestamp, is_stale
        )

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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
OXYGEN_AI_FILE_EOF

echo '  apps/api/app/utils.py'
cat > apps/api/app/utils.py << 'OXYGEN_AI_FILE_EOF'
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def round2(n: float) -> float:
    return round(n * 100) / 100


STALE_THRESHOLD = timedelta(days=2)


def compute_freshness(last_bar_timestamp: str) -> bool:
    """
    True if the most recent bar is stale for a daily timeframe.

    The synthetic generator's last bar is always dated "yesterday"
    relative to generation time (by construction — see
    generate_synthetic_ohlcv), so a fresh generation is always ~1 day
    old the instant it's produced. That's the correct, expected age for
    a daily bar, not staleness. The threshold is 2 days specifically so
    a bar freshly generated a moment ago reads as current, and this only
    trips once a *further* full day passes with no new bar appended —
    e.g. persisted bars nobody has requested (and therefore refreshed)
    in several days.
    """
    ts = datetime.fromisoformat(last_bar_timestamp.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - ts) > STALE_THRESHOLD
OXYGEN_AI_FILE_EOF

echo '  apps/api/app/verify.py'
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
    data_timestamp: str,
    is_stale: bool,
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
        dataTimestamp=data_timestamp,
        isStale=is_stale,
    )


def build_trade_analysis(
    indicators: IndicatorBundle,
    reasoning: ProviderReasoning,
    source: str,
    provider_id: str,
    model_id: str,
    persisted: bool,
    data_timestamp: str,
    is_stale: bool,
) -> TradeAnalysis:
    direction = reasoning.direction
    confidence = reasoning.confidence

    if not direction or indicators.atr14 is None:
        return _no_setup(
            indicators, reasoning, source, provider_id, model_id, persisted, data_timestamp, is_stale
        )

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
            data_timestamp,
            is_stale,
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
        dataTimestamp=data_timestamp,
        isStale=is_stale,
    )
OXYGEN_AI_FILE_EOF

echo '  apps/api/tests/test_comparison.py'
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
        ["mock", "grok", "gemma"],
        "ISOTEST",
        indicators,
        last_bars,
        lambda pid: f"{pid}-model",
        False,
        bars[-1].timestamp,
        False,
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

    results = await run_comparison(
        ["grok", "gemma"],
        "ISOTEST2",
        indicators,
        last_bars,
        lambda pid: f"{pid}-model",
        False,
        bars[-1].timestamp,
        False,
    )

    assert len(results) == 2
    for r in results:
        assert r.outcome == "unavailable"
        assert len(r.reason) > 0

    assert results[0].reason != results[1].reason


@pytest.mark.asyncio
async def test_ok_slots_carry_the_shared_persisted_and_freshness_values():
    # Regression check for the bug fixed alongside this feature: every
    # slot used to hardcode persisted=False regardless of the actual
    # request-level value.
    bars = generate_synthetic_ohlcv("ISOTEST3", 60)
    indicators = compute_indicators(bars)
    last_bars = [{"timestamp": b.timestamp, "close": b.close} for b in bars[-5:]]

    results = await run_comparison(
        ["mock", "grok"],
        "ISOTEST3",
        indicators,
        last_bars,
        lambda pid: f"{pid}-model",
        True,
        bars[-1].timestamp,
        False,
    )

    mock_slot = next(r for r in results if r.providerId == "mock")
    assert mock_slot.outcome == "ok"
    assert mock_slot.analysis.persisted is True
    assert mock_slot.analysis.dataTimestamp == bars[-1].timestamp
    assert mock_slot.analysis.isStale is False
OXYGEN_AI_FILE_EOF

echo '  apps/api/tests/test_scoring.py'
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
        dataTimestamp=now_iso(),
        isStale=False,
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

echo '  apps/api/tests/test_utils.py'
cat > apps/api/tests/test_utils.py << 'OXYGEN_AI_FILE_EOF'
from datetime import datetime, timedelta, timezone

from app.indicators import generate_synthetic_ohlcv
from app.utils import compute_freshness


def test_freshly_generated_bars_are_not_stale():
    # Regression test for a real bug: the synthetic generator's last bar
    # is always dated ~1 day before generation time by construction, so
    # a threshold of exactly 1 day tripped "stale" on every fresh
    # generation by a few microseconds of processing time.
    bars = generate_synthetic_ohlcv("FRESHTEST", 10)
    assert compute_freshness(bars[-1].timestamp) is False


def test_a_bar_several_days_old_is_stale():
    old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    assert compute_freshness(old_ts) is True


def test_a_bar_from_a_moment_ago_is_not_stale():
    fresh_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    assert compute_freshness(fresh_ts) is False
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
OXYGEN_AI_FILE_EOF

echo ""
echo "Done. Next steps:"
echo "  cd apps/api && .venv/bin/mypy app --ignore-missing-imports"
echo "  .venv/bin/pytest tests/ -v"
echo "  cd .. && npx tsc --noEmit && npm run build"