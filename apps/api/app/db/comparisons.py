"""
Port of lib/db/comparisons.ts. Persists a completed comparison: one
ai_comparisons row (provider_set as jsonb — Passage 1 §9.1's fix for
Revision 2's hardcoded two-column design), and one ai_comparison_results
row per participating provider (2 or 3 rows), each linked to its seeded
ai_providers/ai_models rows and, where one exists, the trading_signals
row save_signal() created.
"""

from __future__ import annotations

import json

from .client import get_pool
from .signals import get_or_seed_model
from ..schemas import ComparisonSlot


async def save_comparison(
    provider_ids: list[str],
    scores: dict,
    signal_id_by_provider: dict[str, str | None],
    model_id_by_provider: dict[str, str],
) -> str:
    pool = await get_pool()

    comparison_row = await pool.fetchrow(
        "INSERT INTO ai_comparisons (provider_set, scores) VALUES ($1, $2) RETURNING id",
        json.dumps(provider_ids),
        json.dumps(scores),
    )
    comparison_id = str(comparison_row["id"])

    for provider_id in provider_ids:
        seeded = await get_or_seed_model(
            provider_id, model_id_by_provider.get(provider_id, "unknown"), provider_id
        )
        await pool.execute(
            """
            INSERT INTO ai_comparison_results (comparison_id, provider_id, model_id, result_id)
            VALUES ($1, $2, $3, $4)
            """,
            comparison_id,
            seeded.provider_id,
            seeded.model_db_id,
            signal_id_by_provider.get(provider_id),
        )

    return comparison_id


def build_scores_record(slots: list[ComparisonSlot]) -> dict:
    record: dict = {}
    for slot in slots:
        if slot.outcome == "ok":
            record[slot.providerId] = slot.scores.model_dump()
        else:
            record[slot.providerId] = {"unavailable": slot.reason}
    return record
