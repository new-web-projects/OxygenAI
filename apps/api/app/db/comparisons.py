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


async def get_comparison(comparison_id: str) -> dict | None:
    pool = await get_pool()
    # LEFT JOIN so userChoiceProviderId comes back as the same name
    # string ("grok", "mock", ...) every other providerId in this API
    # uses — not the internal ai_providers.id UUID the column actually
    # stores.
    row = await pool.fetchrow(
        """
        SELECT ac.id, ac.provider_set, ap.name AS user_choice_provider_name, ac.user_rating
        FROM ai_comparisons ac
        LEFT JOIN ai_providers ap ON ap.id = ac.user_choice_provider_id
        WHERE ac.id = $1
        """,
        comparison_id,
    )
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "providerSet": json.loads(row["provider_set"]),
        "userChoiceProviderId": row["user_choice_provider_name"],
        "userRating": row["user_rating"],
    }


async def get_provider_db_id_by_name(name: str) -> str | None:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT id FROM ai_providers WHERE name = $1", name)
    return str(row["id"]) if row else None


async def update_comparison_feedback(
    comparison_id: str, rating: int | None, preferred_provider_db_id: str | None
) -> dict | None:
    """
    Partial update — COALESCE leaves a column unchanged when its
    parameter is None, so a request that only sets one of the two
    (rating or preference) doesn't clobber the other. Re-fetches through
    get_comparison() afterward so the response carries the provider
    *name*, not the raw ai_providers.id the column stores (UPDATE's
    RETURNING can't join another table).
    """
    pool = await get_pool()
    updated = await pool.fetchrow(
        """
        UPDATE ai_comparisons
        SET user_rating = COALESCE($2, user_rating),
            user_choice_provider_id = COALESCE($3, user_choice_provider_id)
        WHERE id = $1
        RETURNING id
        """,
        comparison_id,
        rating,
        preferred_provider_db_id,
    )
    if updated is None:
        return None
    return await get_comparison(comparison_id)
