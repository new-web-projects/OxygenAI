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
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .client import get_pool


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


async def similarity_search(query_vector: list[float], top_k: int) -> list[SearchRow]:
    pool = await get_pool()
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
        ORDER BY e.vector <=> $1::vector
        LIMIT $2
        """,
        _vector_literal(query_vector),
        top_k,
    )
    return [
        SearchRow(
            chunk_id=str(r["chunk_id"]),
            document_id=str(r["document_id"]),
            document_title=r["document_title"],
            source_type=r["source_type"],
            content=r["content"],
            position=r["position"],
            similarity=float(r["similarity"]),
        )
        for r in rows
    ]


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