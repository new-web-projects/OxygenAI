-- 0006_persistence_hygiene.sql
-- Phase P2 of the Passage 1 + Passage 4 continuation (persistence hygiene &
-- operational foundations). No table or column is created or dropped.
--
-- 1. technical_indicators had no natural key, so every POST /api/ai/analyze
--    inserted a fresh copy of the same (instrument, ts, timeframe, indicator)
--    values. Reproduced against a live database: 22 duplicate groups after a
--    handful of requests. Existing duplicates are removed (the newest row of
--    each group is kept) and a UNIQUE index makes the application's upsert
--    (`ON CONFLICT ... DO UPDATE`) possible. The tool that reads this table
--    uses DISTINCT ON (indicator_name) ORDER BY ts DESC, so which duplicate
--    survives does not change what it returns.
--
-- 2. ai_models.display_name repair. Two writers disagreed about the name and the
--    upsert overwrote it on every use: the single-provider path wrote the
--    REQUESTED provider's label (so a Grok request that fell back to mock
--    renamed the (mock, mock-v1) row "Grok (xAI)"), and the comparison path
--    wrote the bare provider id ("grok"). The application no longer overwrites
--    a name on conflict. This statement repairs rows damaged earlier, and only
--    those it can recognise with certainty: the mock-v1 row, and any row whose
--    display_name is just its provider's id.
--
-- error_logs: the partial index on the open backlog already exists from 0001
-- (idx_error_logs_open), so P2 adds none.
--
-- Idempotent: re-running changes nothing. There is deliberately no BEGIN/COMMIT
-- in this file so it can be run inside a caller's transaction (the test suite
-- does exactly that).
--
-- ROLLBACK (the project has no migration runner; run manually if needed):
--   DROP INDEX IF EXISTS uq_technical_indicators_key;
--   -- the duplicate-row removal and the display_name repair are data fixes and
--   -- are not reversible (the removed rows were exact duplicates).

DELETE FROM technical_indicators a
USING technical_indicators b
WHERE a.instrument_id  = b.instrument_id
  AND a.ts             = b.ts
  AND a.timeframe      = b.timeframe
  AND a.indicator_name = b.indicator_name
  AND a.id < b.id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_technical_indicators_key
  ON technical_indicators (instrument_id, ts, timeframe, indicator_name);

UPDATE ai_models m
   SET display_name = 'Mock Reasoner (offline)', updated_at = now()
  FROM ai_providers p
 WHERE p.id = m.provider_id
   AND p.name = 'mock'
   AND m.model_id = 'mock-v1'
   AND m.display_name <> 'Mock Reasoner (offline)';

UPDATE ai_models m
   SET display_name = m.model_id, updated_at = now()
  FROM ai_providers p
 WHERE p.id = m.provider_id
   AND m.display_name = p.name
   AND m.display_name <> m.model_id;
