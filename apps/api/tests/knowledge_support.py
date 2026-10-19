"""Shared helpers for the knowledge-base integration tests (no fixtures here)."""

from __future__ import annotations

ZERO_VECTOR_LITERAL = "[" + ",".join(["0"] * 1536) + "]"
TITLE_PREFIX = "zz-nan-"


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


async def insert_legacy_chunk(pool, document_id: str, position: int, content: str, vector_text: str) -> tuple[str, str]:
    """
    Write a chunk + embedding row DIRECTLY in SQL, bypassing the application
    guards — this reproduces what versions without validation left behind
    (a zero vector for text with no ASCII letters/digits). Returns
    (chunk_id, embedding_id).
    """
    chunk_id = await pool.fetchval(
        "INSERT INTO knowledge_chunks (document_id, content, position, metadata) "
        "VALUES ($1, $2, $3, '{}'::jsonb) RETURNING id",
        document_id,
        content,
        position,
    )
    embedding_id = await pool.fetchval(
        "INSERT INTO embeddings (chunk_id, vector, model_used) VALUES ($1, $2::vector, 'hashing-trick-v1') RETURNING id",
        chunk_id,
        vector_text,
    )
    return str(chunk_id), str(embedding_id)


async def insert_ready_document(pool, title: str) -> str:
    document_id = await pool.fetchval(
        "INSERT INTO knowledge_documents (title, source_type, status) VALUES ($1, 'knowledge', 'ready') RETURNING id",
        title,
    )
    return str(document_id)


async def delete_test_documents(pool) -> None:
    await pool.execute("DELETE FROM knowledge_documents WHERE title LIKE $1", TITLE_PREFIX + "%")
