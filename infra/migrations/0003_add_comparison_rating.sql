-- Adds the one column ai_comparisons was missing for Passage 4 §3.6's
-- "User rating" footer action. user_choice_provider_id (Select
-- preferred) already existed from 0001 — this is the other half.

ALTER TABLE ai_comparisons
  ADD COLUMN user_rating smallint CHECK (user_rating BETWEEN 1 AND 5);
