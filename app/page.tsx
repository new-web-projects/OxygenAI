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
