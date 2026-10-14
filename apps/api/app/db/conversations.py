"""
Conversations — reads and writes the `conversations` table (Passage 4
§5.2/G12: `conversations(id, user_id, mode, created_at)`), which existed
in the schema but had no application code.

First owner of the table is comparison feedback authorization: a
multi-provider comparison run by an authenticated user is attached to a
conversation, and `ai_comparisons.conversation_id` then answers "whose
comparison is this?" so a rating/preference can only be written by its
owner (or an admin). The WebSocket and memory phases build on the same
table for conversation/channel isolation rather than introducing a
second ownership concept.
"""

from __future__ import annotations

from .client import get_pool

VALID_MODES = ("custom", "grok", "gemma", "multi")


async def create_conversation(user_id: str, mode: str) -> str:
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid conversation mode '{mode}' — expected one of {VALID_MODES}")
    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO conversations (user_id, mode) VALUES ($1, $2) RETURNING id", user_id, mode
    )
    assert row is not None
    return str(row["id"])
