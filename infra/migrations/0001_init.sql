-- Oxygen AI — initial database schema
--
-- Source: Blueprint Passage 1 §9.1 (corrected ai_comparisons + two new
--         tables) + Passage 4 §5.2-§5.4 (the full recovered core schema —
--         every table Revision 2 defined that Passage 1 declared
--         "unchanged" without reproducing — plus relationships,
--         constraints, indexing, and the table-name reconciliation
--         reasoning).
--
-- Design choices made explicit here, since the source documents allow
-- either option:
--   - CHECK constraints instead of native Postgres ENUM types for all
--     enum-shaped columns (§5.3 explicitly allows either).
--   - status/enum values are lowercase throughout, for consistency within
--     this file — the blueprint prose sometimes shows 'CLOSED', this
--     schema uses 'closed'. Pick one convention and keep it if you build
--     on this.
--   - Retention (§5.3): "raw tick-level market_data older than N months
--     rolls up to OHLCV bars in cold storage" is an operational policy
--     (a scheduled job), not something a schema file can enforce by
--     itself — it is NOT implemented here, only documented. audit_logs
--     and trade_results are kept indefinitely by design: nothing deletes
--     them (no CASCADE points at them; see the FK notes below).
--
-- Verified against a real PostgreSQL 16 instance — see README for the
-- exact command run and its output. Not yet wired into the application;
-- the Next.js app still runs on synthetic in-memory data. Connecting the
-- two is a separate, larger piece of work (see README).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto; -- gen_random_uuid()

-- ============================================================
-- Identity & access
-- ============================================================

CREATE TABLE roles (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL UNIQUE,
  description text
);

CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         text NOT NULL UNIQUE,
  password_hash text NOT NULL,
  role_id       uuid NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deleted')),
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE permissions (
  id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  role_id  uuid NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  resource text NOT NULL,
  action   text NOT NULL
);

CREATE TABLE sessions (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash text NOT NULL,
  expires_at timestamptz NOT NULL,
  ip         inet,
  user_agent text
);

-- ============================================================
-- AI provider layer
-- ============================================================

CREATE TABLE ai_providers (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name       text NOT NULL UNIQUE, -- 'custom' | 'grok' | 'gemma' — never 'gemini'
  type       text NOT NULL,
  enabled    boolean NOT NULL DEFAULT true,
  is_default boolean NOT NULL DEFAULT false,
  priority   int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- status enum expanded per blueprint §4.5: three providers with different
-- online/offline shapes make disabled/local/hosted/fallback load-bearing
-- for the admin UI, not just inferred from other columns.
CREATE TABLE ai_models (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_id       uuid NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
  model_id          text NOT NULL, -- e.g. 'grok-4.6', 'gemma-4-4b-it'
  display_name      text NOT NULL,
  status            text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'disabled', 'deprecated', 'retired', 'local', 'hosted', 'fallback')),
  context_window    int,
  input_modalities  text[] NOT NULL DEFAULT '{}',
  output_modalities text[] NOT NULL DEFAULT '{}',
  is_default        boolean NOT NULL DEFAULT false,
  is_fallback       boolean NOT NULL DEFAULT false,
  online_capable    boolean NOT NULL DEFAULT true,
  offline_capable   boolean NOT NULL DEFAULT false,
  config            jsonb NOT NULL DEFAULT '{}',
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Folds grok_settings/custom_ai_settings into one generic table keyed by
-- provider_id (§5.4's table-reconciliation reasoning) — now also covering
-- Gemma under the identical pattern.
CREATE TABLE ai_provider_settings (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_id       uuid NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
  api_key_encrypted text,
  timeout_ms        int NOT NULL DEFAULT 30000,
  retry_count       int NOT NULL DEFAULT 2,
  base_url          text,
  region            text,
  updated_by        uuid REFERENCES users(id) ON DELETE SET NULL,
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ai_routing_rules (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  mode                  text NOT NULL CHECK (mode IN ('custom', 'grok', 'gemma', 'multi')),
  provider_priority     jsonb NOT NULL DEFAULT '[]',
  fallback_provider_id  uuid REFERENCES ai_providers(id) ON DELETE SET NULL
);

CREATE TABLE ai_fallback_rules (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_id           uuid NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
  condition             text NOT NULL,
  fallback_provider_id  uuid REFERENCES ai_providers(id) ON DELETE SET NULL,
  fallback_model_id     uuid REFERENCES ai_models(id) ON DELETE SET NULL
);

-- ============================================================
-- Trading domain
-- ============================================================

CREATE TABLE market_instruments (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol          text NOT NULL,
  exchange        text NOT NULL,
  instrument_type text NOT NULL,
  enabled         boolean NOT NULL DEFAULT true,
  metadata        jsonb NOT NULL DEFAULT '{}',
  UNIQUE (symbol, exchange)
);

CREATE TABLE market_data (
  id            bigserial PRIMARY KEY,
  instrument_id uuid NOT NULL REFERENCES market_instruments(id) ON DELETE CASCADE,
  ts            timestamptz NOT NULL,
  open          numeric NOT NULL,
  high          numeric NOT NULL,
  low           numeric NOT NULL,
  close         numeric NOT NULL,
  volume        bigint NOT NULL,
  timeframe     text NOT NULL,
  source        text NOT NULL,
  data_mode     text NOT NULL DEFAULT 'demo' CHECK (data_mode IN ('demo', 'paper', 'live')),
  is_stale      boolean NOT NULL DEFAULT false
);
-- composite index for all time-series tables, per §5.3
CREATE INDEX idx_market_data_ts ON market_data (instrument_id, ts, timeframe);

CREATE TABLE historical_data (
  id            bigserial PRIMARY KEY,
  instrument_id uuid NOT NULL REFERENCES market_instruments(id) ON DELETE CASCADE,
  ts            timestamptz NOT NULL,
  ohlcv         jsonb NOT NULL,
  timeframe     text NOT NULL,
  source        text NOT NULL
);
CREATE INDEX idx_historical_data_ts ON historical_data (instrument_id, ts, timeframe);

CREATE TABLE technical_indicators (
  id             bigserial PRIMARY KEY,
  instrument_id  uuid NOT NULL REFERENCES market_instruments(id) ON DELETE CASCADE,
  ts             timestamptz NOT NULL,
  timeframe      text NOT NULL,
  indicator_name text NOT NULL,
  value          jsonb NOT NULL
);
CREATE INDEX idx_technical_indicators_ts ON technical_indicators (instrument_id, ts, timeframe);

CREATE TABLE strategies (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name      text NOT NULL,
  rules     jsonb NOT NULL DEFAULT '{}',
  owner_id  uuid REFERENCES users(id) ON DELETE SET NULL,
  is_active boolean NOT NULL DEFAULT true
);

CREATE TABLE strategy_versions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id uuid NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  version     int NOT NULL,
  rules       jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (strategy_id, version)
);

-- formula is a constrained expression tree over OHLCV + already-computed
-- base indicators — never arbitrary code (blueprint §4.1 recovered callout).
CREATE TABLE custom_indicator_definitions (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name       text NOT NULL,
  formula    jsonb NOT NULL,
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  status     text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'disabled')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE trading_signals (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  instrument_id uuid NOT NULL REFERENCES market_instruments(id) ON DELETE CASCADE,
  generated_by  uuid REFERENCES ai_models(id) ON DELETE SET NULL,
  direction     text NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
  setup_type    text,
  entry_low     numeric,
  entry_high    numeric,
  stop_loss     numeric NOT NULL,
  target_1      numeric,
  target_2      numeric,
  target_3      numeric,
  risk_reward   numeric,
  confidence    numeric CHECK (confidence >= 0 AND confidence <= 100),
  status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'invalidated')),
  invalidation  text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- RESTRICT: trade_results is the evaluation dataset — a signal it points
-- to must never be silently deletable out from under it (§5.3).
CREATE TABLE trade_setups (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_id uuid NOT NULL REFERENCES trading_signals(id) ON DELETE RESTRICT,
  user_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status    text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
  notes     text
);

CREATE TABLE trade_results (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trade_setup_id uuid NOT NULL REFERENCES trade_setups(id) ON DELETE RESTRICT,
  outcome        text NOT NULL CHECK (outcome IN ('win', 'loss', 'breakeven', 'open')),
  pnl            numeric,
  closed_at      timestamptz,
  notes          text
);

CREATE TABLE backtests (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id  uuid NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  params       jsonb NOT NULL DEFAULT '{}',
  metrics      jsonb,
  equity_curve jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- Knowledge / RAG
-- ============================================================

CREATE TABLE knowledge_documents (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title       text NOT NULL,
  source_type text NOT NULL,
  uploaded_by uuid REFERENCES users(id) ON DELETE SET NULL,
  status      text NOT NULL DEFAULT 'processing' CHECK (status IN ('processing', 'ready', 'failed')),
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_chunks (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
  content     text NOT NULL,
  position    int NOT NULL,
  metadata    jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE embeddings (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chunk_id   uuid NOT NULL REFERENCES knowledge_chunks(id) ON DELETE CASCADE,
  vector     vector(1536) NOT NULL,
  model_used text NOT NULL
);
-- IVFFlat index per §5.3. Needs rows in the table to train well in a real
-- deployment; harmless to create empty here.
CREATE INDEX idx_embeddings_vector ON embeddings USING ivfflat (vector vector_cosine_ops);

-- ============================================================
-- Conversation & memory
-- ============================================================

CREATE TABLE conversations (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  mode       text NOT NULL CHECK (mode IN ('custom', 'grok', 'gemma', 'multi')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE messages (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            text NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
  content         text NOT NULL,
  provider_id     uuid REFERENCES ai_providers(id) ON DELETE SET NULL,
  model_id        uuid REFERENCES ai_models(id) ON DELETE SET NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memories (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  layer      text NOT NULL CHECK (layer IN ('short_term', 'session', 'long_term', 'knowledge', 'trading', 'system')),
  key        text NOT NULL,
  value      jsonb NOT NULL,
  expires_at timestamptz
);

-- ============================================================
-- Tools, comparisons, feedback
-- ============================================================

CREATE TABLE tool_registry (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL UNIQUE,
  description   text,
  input_schema  jsonb NOT NULL,
  output_schema jsonb NOT NULL,
  permissions   jsonb NOT NULL DEFAULT '{}',
  timeout_ms    int NOT NULL DEFAULT 10000,
  rate_limit    int
);

CREATE TABLE tool_calls (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid REFERENCES conversations(id) ON DELETE CASCADE,
  tool_id         uuid NOT NULL REFERENCES tool_registry(id) ON DELETE RESTRICT,
  input           jsonb NOT NULL,
  output          jsonb,
  status          text NOT NULL CHECK (status IN ('ok', 'error', 'timeout')),
  error           text,
  latency_ms      int,
  created_at      timestamptz NOT NULL DEFAULT now()
);

-- Revision 3 fix (Passage 1 §9.1): provider_set jsonb, not the hardcoded
-- custom_result_id/grok_result_id columns Revision 2 had — that hardcoding
-- was invisible at two providers, broken at three.
CREATE TABLE ai_comparisons (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id         uuid REFERENCES conversations(id) ON DELETE CASCADE,
  provider_set            jsonb NOT NULL,
  user_choice_provider_id uuid REFERENCES ai_providers(id) ON DELETE SET NULL,
  scores                  jsonb,
  created_at              timestamptz NOT NULL DEFAULT now()
);

-- one row per participating provider in a comparison (2 or 3 rows)
CREATE TABLE ai_comparison_results (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  comparison_id uuid NOT NULL REFERENCES ai_comparisons(id) ON DELETE CASCADE,
  provider_id   uuid NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
  model_id      uuid REFERENCES ai_models(id) ON DELETE SET NULL,
  result_id     uuid REFERENCES trading_signals(id) ON DELETE SET NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ai_health_checks (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_id  uuid NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
  model_id     uuid REFERENCES ai_models(id) ON DELETE SET NULL,
  checked_at   timestamptz NOT NULL DEFAULT now(),
  status       text NOT NULL CHECK (status IN ('ok', 'degraded', 'down')),
  latency_ms   int,
  error_detail text
);

CREATE TABLE feedback (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  signal_id  uuid REFERENCES trading_signals(id) ON DELETE SET NULL,
  rating     int CHECK (rating BETWEEN 1 AND 5),
  comment    text,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- Ops
-- ============================================================

CREATE TABLE error_logs (
  id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  error_id text NOT NULL,
  ts       timestamptz NOT NULL DEFAULT now(),
  type     text NOT NULL,
  message  text NOT NULL,
  stack    text,
  route    text,
  user_id  uuid REFERENCES users(id) ON DELETE SET NULL,
  provider text,
  severity text NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
  status   text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'ack', 'closed'))
);
-- partial index on the open backlog, per §5.3
CREATE INDEX idx_error_logs_open ON error_logs (status) WHERE status != 'closed';

-- RESTRICT: the audit trail must never lose its actor reference (§5.3).
CREATE TABLE audit_logs (
  id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  action   text NOT NULL,
  resource text NOT NULL,
  before   jsonb,
  after    jsonb,
  ts       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE system_settings (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key        text NOT NULL UNIQUE,
  value      jsonb NOT NULL,
  updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE feature_flags (
  id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key     text NOT NULL UNIQUE,
  enabled boolean NOT NULL DEFAULT false,
  rollout jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE usage_logs (
  id            bigserial PRIMARY KEY,
  user_id       uuid REFERENCES users(id) ON DELETE SET NULL,
  provider_id   uuid REFERENCES ai_providers(id) ON DELETE SET NULL,
  model_id      uuid REFERENCES ai_models(id) ON DELETE SET NULL,
  tokens_in     int,
  tokens_out    int,
  cost_estimate numeric,
  ts            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE voice_settings (
  id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id  uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider text,
  voice    text,
  language text,
  speed    numeric,
  enabled  boolean NOT NULL DEFAULT false
);