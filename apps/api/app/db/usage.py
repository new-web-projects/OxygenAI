"""
`usage_logs` — the first application code to write to this table
(confirmed unused by any code path before this pass). Written only when
`security.dependencies.get_optional_user` resolves a real user on
`/api/ai/analyze`; an anonymous request writes nothing here, exactly as
before this pass.

Token counts and cost are left NULL — no provider integration in this
codebase currently reports them (Grok/Gemma both parse only a text
completion, not a full usage object), and inventing a number would
violate the same "null field, not a fabricated number" rule the trading
engine itself follows. Populating them is a real, scoped follow-up once
a provider's raw response object is threaded through, not a gap in this
table's design.
"""

from __future__ import annotations

from .client import get_pool


async def record_usage(user_id: str, provider_db_id: str | None, model_db_id: str | None) -> None:
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO usage_logs (user_id, provider_id, model_id) VALUES ($1, $2, $3)",
        user_id,
        provider_db_id,
        model_db_id,
    )