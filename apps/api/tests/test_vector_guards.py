"""
Finite-number guards at the storage and response boundaries (no database).
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

import app.db.knowledge as knowledge_db
from app.db.knowledge import apply_embedding_repairs, insert_chunks_with_embeddings, is_usable_vector, similarity_search
from app.schemas import KnowledgeChunkResponse

UNIT = [1.0] + [0.0] * 1535
ZERO = [0.0] * 1536


@pytest.mark.parametrize(
    "vector, usable",
    [
        (UNIT, True),
        ([0.6, 0.8], True),
        ([1, 2, 3], True),  # ints are fine
        (ZERO, False),
        ([], False),
        ([float("nan"), 1.0], False),
        ([float("inf"), 1.0], False),
        ([float("-inf")], False),
        ([1e-9, 0.0], False),  # norm far below float32-safe range
        ([1e30, 1e30], False),  # norm beyond float32-safe range
        ([1e200, 1e200], False),  # squared norm overflows float64
        (["a", "b"], False),  # not numbers
        ([None, 1.0], False),
    ],
)
def test_is_usable_vector(vector, usable):
    assert is_usable_vector(vector) is usable


def test_is_usable_vector_never_raises():
    assert is_usable_vector(object()) is False  # type: ignore[arg-type]


def _chunk_response(score):
    return KnowledgeChunkResponse(
        chunkId="c", documentId="d", documentTitle="t", sourceType="knowledge", content="x", score=score, position=0
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_response_model_rejects_non_finite_scores(bad):
    with pytest.raises(ValidationError):
        _chunk_response(bad)


def test_response_model_accepts_finite_scores_and_serialises_to_valid_json():
    response = _chunk_response(0.5)
    assert response.score == 0.5
    assert '"score":0.5' in response.model_dump_json()


@pytest.mark.asyncio
async def test_storing_an_unusable_vector_is_refused_before_any_database_work(monkeypatch):
    async def must_not_be_called():
        raise AssertionError("the database must not be touched for an invalid vector")

    monkeypatch.setattr(knowledge_db, "get_pool", must_not_be_called)
    with pytest.raises(ValueError, match="chunk 1"):
        await insert_chunks_with_embeddings("doc-id", ["fine", "no signal"], [UNIT, ZERO], "model")


@pytest.mark.asyncio
async def test_a_query_without_a_usable_direction_matches_nothing_without_querying(monkeypatch):
    async def must_not_be_called():
        raise AssertionError("the database must not be queried with an unusable vector")

    monkeypatch.setattr(knowledge_db, "get_pool", must_not_be_called)
    for bad in (ZERO, [math.nan] * 1536, []):
        assert await similarity_search(bad, top_k=5) == []


@pytest.mark.asyncio
async def test_a_repair_with_an_unusable_replacement_is_refused_before_any_database_work(monkeypatch):
    async def must_not_be_called():
        raise AssertionError("the database must not be touched for an invalid replacement vector")

    monkeypatch.setattr(knowledge_db, "get_pool", must_not_be_called)
    with pytest.raises(ValueError, match="not usable"):
        await apply_embedding_repairs([("embedding-id", ZERO)], [], "model")
