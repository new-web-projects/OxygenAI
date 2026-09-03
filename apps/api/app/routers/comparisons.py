"""
Passage 4 §3.6's two mutating comparison footer actions: User rating and
Select preferred result. Both write to the ai_comparisons row the
original POST /api/ai/analyze (multi-provider mode) already created —
this is why that endpoint now returns comparisonId.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..db.client import is_db_configured
from ..db.comparisons import get_comparison, get_provider_db_id_by_name, update_comparison_feedback
from ..schemas import ComparisonFeedback, RateComparisonRequest

router = APIRouter()


@router.patch("/api/ai/comparisons/{comparison_id}")
async def rate_comparison(comparison_id: str, body: RateComparisonRequest) -> ComparisonFeedback:
    if not is_db_configured():
        raise HTTPException(
            status_code=503, detail="Ratings require a database — DATABASE_URL is not set"
        )
    if body.rating is None and body.preferredProviderId is None:
        raise HTTPException(status_code=400, detail="Provide rating and/or preferredProviderId")

    existing = await get_comparison(comparison_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="No comparison found with that id")

    preferred_db_id = None
    if body.preferredProviderId is not None:
        # You can only prefer a provider that was actually part of this
        # comparison — not any provider id that happens to exist.
        if body.preferredProviderId not in existing["providerSet"]:
            raise HTTPException(
                status_code=400,
                detail=f"'{body.preferredProviderId}' was not part of this comparison "
                f"(participants: {', '.join(existing['providerSet'])})",
            )
        preferred_db_id = await get_provider_db_id_by_name(body.preferredProviderId)
        if preferred_db_id is None:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {body.preferredProviderId}")

    updated = await update_comparison_feedback(comparison_id, body.rating, preferred_db_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="No comparison found with that id")

    return ComparisonFeedback(
        comparisonId=updated["id"],
        providerSet=updated["providerSet"],
        userChoiceProviderId=updated["userChoiceProviderId"],
        userRating=updated["userRating"],
    )
