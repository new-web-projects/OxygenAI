-- 0007_provider_registry.sql
-- Phase P3: the database becomes the model registry's system of record
-- (Passage 1 §4.5 / §9.1; Passage 4 §5.2–§5.3). No table is created or dropped.
--
-- Already in place from 0001 and therefore NOT repeated here: the seven-value
-- ai_models.status CHECK (active | disabled | deprecated | retired | local |
-- hosted | fallback), UNIQUE (provider_id, model_id) from 0002, and
-- UNIQUE (system_settings.key).
--
-- 1. ai_models: at most one default and one fallback model per provider, enforced
--    by the database (race-safe) rather than only by application code. A model
--    cannot be both; only models the router may actually call can be either.
-- 2. ai_provider_settings: one row per provider, and timeout_ms / retry_count
--    become genuinely optional overrides (NULL = "no override, use the
--    environment / transport default"). 0001 declared them NOT NULL with
--    defaults (30000 / 2), which would have turned a mere row's existence into an
--    override of the operator's PROVIDER_TIMEOUT_SECONDS / PROVIDER_RETRY_COUNT.
--    The table has had no writer until this phase, so this changes no live data.
--
-- Idempotent. Existing rows that would violate a new rule are repaired first
-- (the newest default/fallback per provider wins). No BEGIN/COMMIT so a caller
-- (the test suite) can run it inside its own transaction.
--
-- ROLLBACK (manual; the project has no migration runner):
--   DROP INDEX IF EXISTS uq_ai_models_one_default_per_provider;
--   DROP INDEX IF EXISTS uq_ai_models_one_fallback_per_provider;
--   DROP INDEX IF EXISTS uq_ai_provider_settings_provider;
--   ALTER TABLE ai_models DROP CONSTRAINT IF EXISTS ai_models_default_not_fallback,
--     DROP CONSTRAINT IF EXISTS ai_models_default_is_callable,
--     DROP CONSTRAINT IF EXISTS ai_models_fallback_is_callable;
--   ALTER TABLE ai_provider_settings DROP CONSTRAINT IF EXISTS ai_provider_settings_timeout_range,
--     DROP CONSTRAINT IF EXISTS ai_provider_settings_retry_range;
--   -- (the NOT NULL / DEFAULT removal on timeout_ms / retry_count is not worth reverting)

-- ---- ai_models ------------------------------------------------------------

UPDATE ai_models SET is_default = false
 WHERE is_default AND status NOT IN ('active', 'hosted', 'local');
UPDATE ai_models SET is_fallback = false
 WHERE is_fallback AND status NOT IN ('active', 'hosted', 'local', 'fallback');
UPDATE ai_models SET is_fallback = false WHERE is_fallback AND is_default;

UPDATE ai_models SET is_default = false
 WHERE is_default
   AND id NOT IN (SELECT DISTINCT ON (provider_id) id FROM ai_models WHERE is_default
                   ORDER BY provider_id, updated_at DESC, id);
UPDATE ai_models SET is_fallback = false
 WHERE is_fallback
   AND id NOT IN (SELECT DISTINCT ON (provider_id) id FROM ai_models WHERE is_fallback
                   ORDER BY provider_id, updated_at DESC, id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_models_one_default_per_provider
  ON ai_models (provider_id) WHERE is_default;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_models_one_fallback_per_provider
  ON ai_models (provider_id) WHERE is_fallback;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_models_default_not_fallback') THEN
    ALTER TABLE ai_models ADD CONSTRAINT ai_models_default_not_fallback
      CHECK (NOT (is_default AND is_fallback));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_models_default_is_callable') THEN
    ALTER TABLE ai_models ADD CONSTRAINT ai_models_default_is_callable
      CHECK (NOT is_default OR status IN ('active', 'hosted', 'local'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_models_fallback_is_callable') THEN
    ALTER TABLE ai_models ADD CONSTRAINT ai_models_fallback_is_callable
      CHECK (NOT is_fallback OR status IN ('active', 'hosted', 'local', 'fallback'));
  END IF;
END $$;

-- ---- ai_provider_settings ---------------------------------------------------

DELETE FROM ai_provider_settings s
 USING ai_provider_settings newer
 WHERE s.provider_id = newer.provider_id
   AND (s.updated_at, s.id) < (newer.updated_at, newer.id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_provider_settings_provider
  ON ai_provider_settings (provider_id);

ALTER TABLE ai_provider_settings ALTER COLUMN timeout_ms DROP NOT NULL;
ALTER TABLE ai_provider_settings ALTER COLUMN timeout_ms DROP DEFAULT;
ALTER TABLE ai_provider_settings ALTER COLUMN retry_count DROP NOT NULL;
ALTER TABLE ai_provider_settings ALTER COLUMN retry_count DROP DEFAULT;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_provider_settings_timeout_range') THEN
    ALTER TABLE ai_provider_settings ADD CONSTRAINT ai_provider_settings_timeout_range
      CHECK (timeout_ms IS NULL OR timeout_ms BETWEEN 100 AND 300000);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_provider_settings_retry_range') THEN
    ALTER TABLE ai_provider_settings ADD CONSTRAINT ai_provider_settings_retry_range
      CHECK (retry_count IS NULL OR retry_count BETWEEN 0 AND 10);
  END IF;
END $$;
