"""
Orchestrates the full Passage 1 section 7 pipeline:

  UPLOAD -> TEXT EXTRACTION -> CLEANING -> CHUNKING -> EMBEDDING ->
  pgvector STORAGE -> METADATA STORAGE -> RETRIEVAL -> RERANKING ->
  LABELED AI CONTEXT

`ingest_text` covers everything from cleaning through storage;
`extract_text` (chunking.py) runs first, in the router, since it needs
the raw upload bytes and content-type that only the router layer has.
`retrieve` covers retrieval through labelling - the source_type tag
Passage 1 section 7 requires survives from `knowledge_documents.
source_type` all the way into the returned dict's `sourceType` key,
which `providers/prompt.py`'s `_knowledge_block` already reads.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db.knowledge import (
    create_document,
    insert_chunks_with_embeddings,
    mark_document_status,
    similarity_search,
)
from ..observability import get_logger
from .chunking import Chunk, chunk_text, clean_text
from .embeddings import get_embedding_provider
from .rerank import RetrievedChunk, rerank

logger = get_logger("rag.pipeline")


@dataclass(frozen=True)
class IngestResult:
    document_id: str
    chunk_count: int
    embedding_model: str


async def ingest_text(
    title: str, raw_text: str, source_type: str, uploaded_by: str | None = None
) -> IngestResult:
    """
    Cleaning through storage. Marks the document 'failed' rather than
    leaving it stuck at 'processing' if chunking or embedding raises -
    a partially-ingested document should never look indistinguishable
    from one still in progress.
    """
    document_id = await create_document(title, source_type, uploaded_by)
    try:
        cleaned = clean_text(raw_text)
        chunks: list[Chunk] = chunk_text(cleaned)
        if not chunks:
            await mark_document_status(document_id, "failed")
            raise ValueError("No content to ingest after cleaning — the upload was empty or all whitespace.")

        provider = get_embedding_provider()
        vectors = await provider.embed_batch([c.content for c in chunks])
        await insert_chunks_with_embeddings(
            document_id, [c.content for c in chunks], vectors, provider.model_name
        )
        await mark_document_status(document_id, "ready")
        logger.info(
            "document ingested",
            extra={"documentId": document_id, "chunks": len(chunks), "model": provider.model_name},
        )
        return IngestResult(document_id=document_id, chunk_count=len(chunks), embedding_model=provider.model_name)
    except Exception:
        await mark_document_status(document_id, "failed")
        raise


async def retrieve(query: str, top_k: int = 5) -> list[dict]:
    """
    Retrieval through labelling. Returns plain dicts (not
    KnowledgeChunkResponse) so this function has no FastAPI/Pydantic
    dependency and can be called equally from `routers/knowledge.py`,
    `routers/analyze.py`'s AnalysisContext.knowledge, and
    `tools/knowledge_tools.py` — three different call sites that each
    shape their own response around this same data.
    """
    provider = get_embedding_provider()
    query_vector = await provider.embed(query)

    # Over-fetch before reranking so the lexical-overlap signal has more
    # than top_k candidates to actually reorder — reranking a set no
    # bigger than what you'd return anyway cannot change the outcome.
    raw = await similarity_search(query_vector, top_k=max(top_k * 3, top_k))
    candidates = [
        RetrievedChunk(
            chunk_id=r.chunk_id,
            document_id=r.document_id,
            document_title=r.document_title,
            source_type=r.source_type,
            content=r.content,
            position=r.position,
            vector_score=r.similarity,
        )
        for r in raw
    ]
    ranked = rerank(query, candidates)[:top_k]

    return [
        {
            "chunkId": c.chunk_id,
            "documentId": c.document_id,
            "documentTitle": c.document_title,
            "sourceType": c.source_type,
            "content": c.content,
            "position": c.position,
            "score": c.vector_score,
        }
        for c in ranked
    ]