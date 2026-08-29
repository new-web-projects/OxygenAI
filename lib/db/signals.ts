import { getPool } from "./client";
import type { TradeAnalysis } from "../types";

const PROVIDER_TYPE: Record<string, string> = {
  mock: "mock",
  custom: "custom",
  grok: "xai",
  gemma: "google", // provider identity is Gemma 4; this is only a type label
};

/**
 * Upserts the ai_providers/ai_models rows this app's four provider ids map
 * to, and returns the ai_models.id foreign key trading_signals.generated_by
 * needs. Requires the 0002 migration (unique constraint on
 * (provider_id, model_id)) — without it this would insert a duplicate
 * model row on every call instead of updating one.
 */
export async function getOrSeedModel(
  providerName: string,
  modelId: string,
  displayName: string
): Promise<string> {
  const pool = getPool();

  const providerResult = await pool.query<{ id: string }>(
    `INSERT INTO ai_providers (name, type)
     VALUES ($1, $2)
     ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
     RETURNING id`,
    [providerName, PROVIDER_TYPE[providerName] ?? providerName]
  );
  const providerId = providerResult.rows[0].id;

  const modelResult = await pool.query<{ id: string }>(
    `INSERT INTO ai_models (provider_id, model_id, display_name)
     VALUES ($1, $2, $3)
     ON CONFLICT (provider_id, model_id) DO UPDATE SET display_name = EXCLUDED.display_name
     RETURNING id`,
    [providerId, modelId, displayName]
  );
  return modelResult.rows[0].id;
}

/**
 * Only SETUP_FOUND results become a trading_signals row — a signal is a
 * specific concrete claim; NO_VALID_SETUP is an absence of one, not a
 * fact worth a row of its own.
 */
export async function saveSignal(
  instrumentId: string,
  modelDbId: string,
  analysis: TradeAnalysis
): Promise<void> {
  if (analysis.status !== "SETUP_FOUND" || !analysis.direction) return;
  const pool = getPool();
  await pool.query(
    `INSERT INTO trading_signals
       (instrument_id, generated_by, direction, entry_low, entry_high,
        stop_loss, target_1, target_2, risk_reward, confidence, status)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'open')`,
    [
      instrumentId,
      modelDbId,
      analysis.direction,
      analysis.entry, // entry_low
      analysis.entry, // entry_high — a point estimate, represented as a zero-width range
      analysis.stopLoss,
      analysis.targets[0] ?? null,
      analysis.targets[1] ?? null,
      analysis.riskReward,
      analysis.confidence,
    ]
  );
}