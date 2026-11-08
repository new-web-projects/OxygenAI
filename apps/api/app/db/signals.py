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


async def get_or_seed_provider(provider_name: str) -> str:
    """Upserts the ai_providers row for `provider_name` and returns its id."""
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
    return str(provider_row["id"])


def _default_display_name(provider_name: str, model_id: str) -> str:
    """The model catalogue's own name for a known model, else the model id itself."""
    from ..providers.model_registry import get_model_registry

    entry = get_model_registry().get(provider_name, model_id)
    return entry.display_name if entry is not None else model_id


async def get_or_seed_model(
    provider_name: str, model_id: str, display_name: str | None = None
) -> SeededModel:
    """
    Upserts the ai_providers/ai_models rows this app's four provider ids
    map to, and returns both ids — ai_models.id for
    trading_signals.generated_by, ai_providers.id for
    ai_comparison_results.provider_id. Requires the 0002 migration
    (unique constraint on (provider_id, model_id)) — without it this
    would insert a duplicate model row on every call instead of updating
    one.

    `display_name` is only used when the row is CREATED. An existing row's name
    is never overwritten: the two callers used to disagree (the requested
    provider's label vs. the bare provider id) and the last writer won on every
    request, so a Grok request that fell back to mock renamed the mock-v1 row
    "Grok (xAI)". With the database becoming the model registry's system of
    record, a name an admin sets must not be reverted by ordinary traffic
    either. When omitted, the catalogue's name for a known model is used.
    """
    provider_id = await get_or_seed_provider(provider_name)
    pool = await get_pool()
    model_row = await pool.fetchrow(
        """
        INSERT INTO ai_models (provider_id, model_id, display_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (provider_id, model_id) DO UPDATE SET model_id = ai_models.model_id
        RETURNING id
        """,
        provider_id,
        model_id,
        display_name or _default_display_name(provider_name, model_id),
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
