-- Fixes a gap in 0001_init.sql, caught while wiring the API route to the
-- database: lib/db/signals.ts upserts an ai_models row on every request
-- that persists a signal (ON CONFLICT (provider_id, model_id) DO UPDATE).
-- Without this constraint, that's not an upsert — it's a new duplicate
-- row every time. 0001 defines provider_id and model_id but never
-- constrained the pair to be unique together. Caught, not designed
-- around: the fix belongs in the schema, not worked around in
-- application code.

ALTER TABLE ai_models
  ADD CONSTRAINT ai_models_provider_model_unique UNIQUE (provider_id, model_id);