"""
Port of lib/db/signals.ts.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import get_pool
from ..schemas import TradeAnalysis

PROVIDER_TYPE: dict[str, str] = {
    "mock": "mock",
    "custom": "custom",
    "grok": "xai",
    "gemma": "google",  # provider identity is Gemma 4; this is only a type label
}


@dataclass
class SeededModel:
    provider_id: str  # ai_providers.id
    model_db_id: str  # ai_models.id


async def get_or_seed_model(provider_name: str, model_id: str, display_name: str) -> SeededModel:
    """
    Upserts the ai_providers/ai_models rows this app's four provider ids
    map to, and returns both ids — ai_models.id for
    trading_signals.generated_by, ai_providers.id for
    ai_comparison_results.provider_id. Requires the 0002 migration
    (unique constraint on (provider_id, model_id)) — without it this
    would insert a duplicate model row on every call instead of updating
    one.
    """
    pool = await get_pool()
    provider_row = await pool.fetchrow(
        """
        INSERT INTO ai_providers (name, type)
        VALUES ($1, $2)
        ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        provider_name,
        PROVIDER_TYPE.get(provider_name, provider_name),
    )
    provider_id = str(provider_row["id"])

    model_row = await pool.fetchrow(
        """
        INSERT INTO ai_models (provider_id, model_id, display_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (provider_id, model_id) DO UPDATE SET display_name = EXCLUDED.display_name
        RETURNING id
        """,
        provider_id,
        model_id,
        display_name,
    )
    return SeededModel(provider_id=provider_id, model_db_id=str(model_row["id"]))


async def save_signal(instrument_id: str, model_db_id: str, analysis: TradeAnalysis) -> str | None:
    """
    Only SETUP_FOUND results become a trading_signals row — a signal is a
    specific concrete claim; NO_VALID_SETUP is an absence of one, not a
    fact worth a row of its own. Returns the new row's id (or None when
    nothing was inserted) so callers like ai_comparison_results can link
    to it.
    """
    if analysis.status != "SETUP_FOUND" or not analysis.direction:
        return None
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO trading_signals
          (instrument_id, generated_by, direction, entry_low, entry_high,
           stop_loss, target_1, target_2, risk_reward, confidence, status)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'open')
        RETURNING id
        """,
        instrument_id,
        model_db_id,
        analysis.direction,
        analysis.entry,  # entry_low
        analysis.entry,  # entry_high — a point estimate, represented as a zero-width range
        analysis.stopLoss,
        analysis.targets[0] if len(analysis.targets) > 0 else None,
        analysis.targets[1] if len(analysis.targets) > 1 else None,
        analysis.riskReward,
        analysis.confidence,
    )
    return str(row["id"])
