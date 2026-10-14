-- 0005_auth_hardening.sql
-- Phase 1 of the Passage 1 + Passage 4 continuation (authentication
-- enforcement). Index-only; no table or column is created, altered, or
-- dropped, so no data is touched and it is safe to apply to a database
-- that already holds users, sessions and comparisons.
--
-- Why these indexes (each query below was a sequential scan):
--
--   * sessions(token_hash): every authenticated request runs
--       SELECT ... FROM sessions WHERE token_hash = $1 AND expires_at > now()
--     Authentication is now mandatory on /api/ai/analyze, so this runs on
--     the hot path. UNIQUE as well as indexed: a token hash identifies one
--     session by construction (the JWT carries a random jti), and a
--     duplicate would make logout revoke only one of two live sessions.
--   * sessions(expires_at): expired-session purge and the validity check.
--   * sessions(user_id): "log out everywhere" (delete_all_sessions_for_user).
--   * conversations(user_id): conversation ownership lookups used by
--     comparison-feedback authorization and the later WebSocket/memory phases.
--
-- Idempotent: every statement is IF NOT EXISTS, so re-running is harmless.
--
-- ROLLBACK (the project has no migration runner; run manually if needed):
--   DROP INDEX IF EXISTS uq_sessions_token_hash;
--   DROP INDEX IF EXISTS idx_sessions_expires_at;
--   DROP INDEX IF EXISTS idx_sessions_user_id;
--   DROP INDEX IF EXISTS idx_conversations_user_id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_token_hash ON sessions (token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions (expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations (user_id);
