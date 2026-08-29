# Oxygen AI — minimal end-to-end slice

A small, real, working slice of the Oxygen AI blueprint — not the full
three-provider / C++/CUDA / 30-table system. It exists to prove the one idea
that actually matters architecturally: **indicators are computed
deterministically, the AI layer only reasons over them, and every provider
sits behind one swappable interface.**

## What's actually real here

- `lib/indicators.ts` — SMA, RSI(14), ATR(14): real formulas, unit-tested.
- `lib/verify.ts` — the verification stage. Entry/stop/targets are computed
  from ATR here, never taken from the AI's text. A consistency check (a LONG
  setup's stop must sit below entry, a SHORT's above) runs before anything
  is returned — fails closed to `NO_VALID_SETUP` instead of shipping a
  broken setup.
- `lib/types.ts` — the `TradeAnalysis` schema (Zod), matching the
  blueprint's schema-validated-output and source-tagging
  (`mock` / `hosted_api` / `local_model`) rules.
- `lib/providers/` — the `AIProvider` interface, a working offline `mock`
  provider (rule-based on the real computed indicators, no key needed), and
  a `gemini` provider that calls Gemma 4 via `@google/genai`.
- One route (`POST /api/analyze`) and one page that run the whole pipeline
  end to end, with the fallback-to-mock behavior the blueprint's router
  itself specifies for an unconfigured provider.

## What's NOT real yet — on purpose

- **Market data is synthetic.** `generateSyntheticOHLCV()` makes up a
  deterministic pseudo-random price series per symbol. No NSE/BSE vendor is
  wired in (TrueData/Global Datafeeds per the blueprint) — that needs a
  paid account only you can set up.
- **The Gemma/Gemini call is untested by me.** This sandbox can't reach
  `generativelanguage.googleapis.com` — outbound network here is locked to
  package registries. The code compiles and type-checks cleanly against the
  real SDK (`tsc --noEmit` passes), and requesting it without a key falls
  back to Mock correctly (verified below) — but the live round-trip needs a
  real `GEMINI_API_KEY` (free, from https://aistudio.google.com/apikey) and
  a run on your machine. `GEMMA_MODEL_ID` may need adjusting — Gemma 4's
  model ID strings have shifted between release waves; check
  https://ai.google.dev/gemma/docs/core/model_card_4 for the current one.
- **No Grok / Custom AI provider yet.** The interface is built so adding
  either is a new file in `lib/providers/`, not a redesign — copy
  `geminiProvider.ts`'s shape.
- **No database.** Nothing is persisted; every request is stateless.

## Running it

```bash
npm install
cp .env.example .env.local   # optional — add GEMINI_API_KEY to use Gemma 4 for real
npm run dev
```

Open http://localhost:3000, type a symbol, hit Analyze. It works immediately
with Mock; switch the dropdown to "Gemma 4" once you've added a key.

## Verified before delivery (actually run, not claimed)

```
$ npm install                     → 148 packages, exit 0
$ npx tsx --test lib/indicators.test.ts   → 9/9 pass
$ npx tsc --noEmit                 → clean, no errors
$ npx next build                   → compiled successfully
$ npx next start, then:
  POST /api/analyze {symbol: RELIANCE, provider: mock}
    → 200, SETUP_FOUND, LONG, entry 409.43, stop 403.6, targets [415.26, 421.09]
  POST /api/analyze {symbol: TCS, provider: gemini}   (no GEMINI_API_KEY set)
    → 200, correctly fell back to source: mock, provider: mock
  POST /api/analyze {}               → 400 (request validation works)
```

Not verified: an actual live call to the Gemini API — blocked by this
sandbox's network policy, not by anything in the code. That's the one thing
you need to confirm yourself.

## If you want to keep going

In order of what's actually next: (1) get a live Gemini key working locally
and confirm `geminiProvider.ts` really round-trips, (2) swap
`generateSyntheticOHLCV` for a real data source — even a free one, before
paying for TrueData, (3) add a second provider (Grok is the cheapest to add
next — same interface, an xAI key), (4) only once two real providers exist
does the blueprint's comparison UI and scoring axes start to mean anything.
