"""
knowledge_documents, knowledge_chunks, embeddings - Passage 1 section 7's
storage stages. All three tables existed before this pass (Passage 4
section 5.2/G12) and were confirmed unused by any code path in the
original audit; this is the first application code to use them.

The similarity search uses pgvector's cosine-distance operator (<=>),
matching the ivfflat index already built with vector_cosine_ops -
confirmed by inspecting the live index definition before writing this
query, not assumed. Distance is converted to a similarity score (1 -
distance) so callers work with the same "higher is better" convention
`rag/rerank.py` uses for its vector_score field.

Cosine similarity is UNDEFINED for a zero-norm vector: pgvector returns
NaN for `<=>` when either side is all zeros (verified against pgvector
0.6.0). A NaN similarity cannot be ordered, cannot be serialised to JSON,
and — before this was handled — made `POST /api/knowledge/search` fail with
"Out of range float values are not JSON compliant: nan". Three rules keep
it out of the system: (1) nothing unusable is ever stored
(`insert_chunks_with_embeddings`), (2) a query with no usable direction
matches nothing (`similarity_search`), and (3) any row whose similarity is
undefined — e.g. a zero-vector row written by an earlier version — is
excluded in SQL rather than returned.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..observability import get_logger
from .client import get_pool

logger = get_logger("db.knowledge")

# Bounds on a vector's L2 norm for it to be usable with cosine distance.
# Real embeddings sit near 1 (the hashing embedder L2-normalises; Google's
# are ~unit length). The bounds only reject the degenerate cases: zero (no
# direction) and values so extreme that pgvector's float32 squared-norm
# accumulation under/overflows to 0/inf and turns the distance into NaN.
MIN_VECTOR_NORM = 1e-6
MAX_VECTOR_NORM = 1e18


def is_usable_vector(vector: Sequence[float]) -> bool:
    """
    True when `vector` has a well-defined direction: every component is a
    finite number and the norm is neither (near) zero nor extreme.
    Never raises — a malformed vector is simply "not usable".
    """
    total = 0.0
    try:
        for component in vector:
            value = float(component)
            if not math.isfinite(value):
                return False
            total += value * value
    except (TypeError, ValueError):
        return False
    if not math.isfinite(total):  # squared-norm overflow
        return False
    return MIN_VECTOR_NORM <= math.sqrt(total) <= MAX_VECTOR_NORM


@dataclass(frozen=True)
class StoredChunk:
    chunk_id: str
    position: int


def _vector_literal(vector: list[float]) -> str:
    """pgvector accepts a vector as the text form '[v1,v2,...]' cast to ::vector."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


async def create_document(title: str, source_type: str, uploaded_by: str | None) -> str:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO knowledge_documents (title, source_type, uploaded_by, status)
        VALUES ($1, $2, $3, 'processing')
        RETURNING id
        """,
        title,
        source_type,
        uploaded_by,
    )
    assert row is not None
    return str(row["id"])


async def mark_document_status(document_id: str, status: str) -> None:
    pool = await get_pool()
    await pool.execute("UPDATE knowledge_documents SET status = $1 WHERE id = $2", status, document_id)


async def insert_chunks_with_embeddings(
    document_id: str,
    chunks: list[str],
    vectors: list[list[float]],
    model_used: str,
) -> list[StoredChunk]:
    if len(chunks) != len(vectors):
        raise ValueError("chunks and vectors must be the same length")
    # Validate everything BEFORE opening the transaction so a bad vector
    # can never leave a half-written document behind.
    for index, vector in enumerate(vectors):
        if not is_usable_vector(vector):
            raise ValueError(
                f"Embedding for chunk {index} has no usable direction "
                "(all-zero, non-finite, or out of range) and cannot be stored — "
                "cosine similarity against it would be NaN"
            )
    pool = await get_pool()
    stored: list[StoredChunk] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for position, (content, vector) in enumerate(zip(chunks, vectors)):
                chunk_row = await conn.fetchrow(
                    """
                    INSERT INTO knowledge_chunks (document_id, content, position, metadata)
                    VALUES ($1, $2, $3, $4::jsonb)
                    RETURNING id
                    """,
                    document_id,
                    content,
                    position,
                    json.dumps({}),
                )
                chunk_id = str(chunk_row["id"])
                await conn.execute(
                    """
                    INSERT INTO embeddings (chunk_id, vector, model_used)
                    VALUES ($1, $2::vector, $3)
                    """,
                    chunk_id,
                    _vector_literal(vector),
                    model_used,
                )
                stored.append(StoredChunk(chunk_id=chunk_id, position=position))
    return stored


@dataclass(frozen=True)
class SearchRow:
    chunk_id: str
    document_id: str
    document_title: str
    source_type: str
    content: str
    position: int
    similarity: float


@dataclass(frozen=True)
class UnusableEmbedding:
    embedding_id: str
    chunk_id: str
    content: str


async def list_unusable_embeddings(document_ids: Sequence[str] | None = None) -> list[UnusableEmbedding]:
    """
    Stored embeddings with no usable direction — zero vectors written by
    versions that did not validate (see the module docstring). Optionally
    limited to the given documents.

    A vector's cosine distance to ITSELF is exactly 0 for any real vector
    and NaN only when its norm is zero, so this finds them without relying
    on a pgvector helper function.
    """
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT e.id AS embedding_id, kc.id AS chunk_id, kc.content
        FROM embeddings e
        JOIN knowledge_chunks kc ON kc.id = e.chunk_id
        WHERE (e.vector <=> e.vector) = 'NaN'::float8
          AND ($1::uuid[] IS NULL OR kc.document_id = ANY($1::uuid[]))
        ORDER BY kc.document_id, kc.position
        """,
        list(document_ids) if document_ids is not None else None,
    )
    return [
        UnusableEmbedding(str(r["embedding_id"]), str(r["chunk_id"]), r["content"]) for r in rows
    ]


async def apply_embedding_repairs(
    replacements: list[tuple[str, list[float]]], deletions: list[str], model_used: str
) -> None:
    """Replace the vectors of some embedding rows and delete others, atomically."""
    for embedding_id, vector in replacements:
        if not is_usable_vector(vector):
            raise ValueError(f"Replacement vector for embedding {embedding_id} is not usable")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for embedding_id, vector in replacements:
                await conn.execute(
                    "UPDATE embeddings SET vector = $1::vector, model_used = $2 WHERE id = $3",
                    _vector_literal(vector),
                    model_used,
                    embedding_id,
                )
            for embedding_id in deletions:
                await conn.execute("DELETE FROM embeddings WHERE id = $1", embedding_id)


async def similarity_search(query_vector: list[float], top_k: int) -> list[SearchRow]:
    """
    Top-`top_k` chunks by cosine similarity. Every returned `similarity` is
    a finite number in [-1, 1]. A query vector with no usable direction
    matches nothing (an empty list), and rows whose similarity is undefined
    are never returned.
    """
    if not is_usable_vector(query_vector):
        return []

    pool = await get_pool()
    # `<> 'NaN'::float8` removes rows whose cosine distance is NaN, i.e. a
    # zero-vector row stored by an earlier version. Postgres treats NaN as
    # equal to itself (unlike IEEE-754), so this predicate is exactly "is
    # not NaN" and needs no pgvector function, so it works on any pgvector
    # version. NaN sorts LAST in ascending order, so these rows could never
    # displace a real match from the LIMIT; excluding them just keeps the
    # undefined value from being returned at all.
    rows = await pool.fetch(
        """
        SELECT
            kc.id AS chunk_id, kc.document_id, kd.title AS document_title,
            kd.source_type, kc.content, kc.position,
            1 - (e.vector <=> $1::vector) AS similarity
        FROM embeddings e
        JOIN knowledge_chunks kc ON kc.id = e.chunk_id
        JOIN knowledge_documents kd ON kd.id = kc.document_id
        WHERE kd.status = 'ready'
          AND (e.vector <=> $1::vector) <> 'NaN'::float8
        ORDER BY e.vector <=> $1::vector
        LIMIT $2
        """,
        _vector_literal(query_vector),
        top_k,
    )

    results: list[SearchRow] = []
    for r in rows:
        similarity = float(r["similarity"])
        if not math.isfinite(similarity):  # defence in depth; the SQL filter should make this unreachable
            logger.warning("dropping a chunk with a non-finite similarity", extra={"chunkId": str(r["chunk_id"])})
            continue
        results.append(
            SearchRow(
                chunk_id=str(r["chunk_id"]),
                document_id=str(r["document_id"]),
                document_title=r["document_title"],
                source_type=r["source_type"],
                content=r["content"],
                position=r["position"],
                # 1 - distance can land a hair outside [-1, 1] from float rounding.
                similarity=max(-1.0, min(1.0, similarity)),
            )
        )
    return results


async def list_documents(limit: int = 50) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT kd.id, kd.title, kd.source_type, kd.status, kd.created_at,
               count(kc.id) AS chunk_count
        FROM knowledge_documents kd
        LEFT JOIN knowledge_chunks kc ON kc.document_id = kd.id
        GROUP BY kd.id
        ORDER BY kd.created_at DESC
        LIMIT $1
        """,
        limit,
    )
    return [
        {
            "id": str(r["id"]),
            "title": r["title"],
            "sourceType": r["source_type"],
            "status": r["status"],
            "chunkCount": r["chunk_count"],
            "createdAt": r["created_at"].isoformat(),
        }
        for r in rows
    ]


async def delete_document(document_id: str) -> bool:
    pool = await get_pool()
    result = await pool.execute("DELETE FROM knowledge_documents WHERE id = $1", document_id)
    return result != "DELETE 0"