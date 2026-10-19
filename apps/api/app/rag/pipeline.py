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

import math
from dataclasses import dataclass

from ..db.knowledge import (
    create_document,
    insert_chunks_with_embeddings,
    is_usable_vector,
    mark_document_status,
    similarity_search,
)
from ..observability import get_logger
from .chunking import Chunk, chunk_text, clean_text
from .embeddings import get_embedding_provider
from .rerank import RetrievedChunk, rerank

logger = get_logger("rag.pipeline")


class NoIndexableContentError(ValueError):
    """
    The upload has nothing that can be searched: empty after cleaning, or
    no chunk produced an embedding with a usable direction (e.g. only
    punctuation or emoji). A ValueError subclass, so existing
    `except ValueError` callers keep working; the HTTP layer maps it to 400
    because it is a problem with the upload, not a server fault.
    """


@dataclass(frozen=True)
class IngestResult:
    document_id: str
    chunk_count: int  # chunks actually stored (and therefore searchable)
    embedding_model: str
    skipped_chunks: int = 0  # chunks dropped because they had nothing embeddable


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
            raise NoIndexableContentError(
                "No content to ingest after cleaning — the upload was empty or all whitespace."
            )

        provider = get_embedding_provider()
        vectors = await provider.embed_batch([c.content for c in chunks])
        if len(vectors) != len(chunks):
            raise ValueError(
                f"Embedding provider returned {len(vectors)} vectors for {len(chunks)} chunks"
            )

        # A chunk whose embedding has no usable direction (nothing but
        # punctuation/emoji, or a degenerate provider response) cannot be
        # ranked by cosine similarity — storing it would later make every
        # search that touches it fail with a NaN score. Skip it rather
        # than store it; it had no searchable content anyway.
        kept = [(c, v) for c, v in zip(chunks, vectors) if is_usable_vector(v)]
        skipped = len(chunks) - len(kept)
        if not kept:
            raise NoIndexableContentError(
                "None of the document's text could be indexed — it contains no letters or "
                "digits to search on (only punctuation, symbols, or whitespace)."
            )
        if skipped:
            logger.warning(
                "skipped chunks with no embeddable content",
                extra={"documentId": document_id, "skipped": skipped, "kept": len(kept)},
            )

        await insert_chunks_with_embeddings(
            document_id, [c.content for c, _ in kept], [v for _, v in kept], provider.model_name
        )
        await mark_document_status(document_id, "ready")
        logger.info(
            "document ingested",
            extra={"documentId": document_id, "chunks": len(kept), "skipped": skipped, "model": provider.model_name},
        )
        return IngestResult(
            document_id=document_id,
            chunk_count=len(kept),
            embedding_model=provider.model_name,
            skipped_chunks=skipped,
        )
    except Exception:
        # Every failure above — including NoIndexableContentError — lands the
        # document in 'failed' here, exactly once.
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
    if not is_usable_vector(query_vector):
        # No letters/digits in the query (e.g. "???"): nothing to match on,
        # and cosine similarity against a zero vector is NaN for every row.
        logger.info("query has no embeddable content; returning no results")
        return []

    # Over-fetch before reranking so the lexical-overlap signal has more
    # than top_k candidates to actually reorder — reranking a set no
    # bigger than what you'd return anyway cannot change the outcome.
    raw = await similarity_search(query_vector, top_k=max(top_k * 3, top_k))
    # Boundary guarantee for every caller (the search endpoint, the analysis
    # prompt, the knowledge tool): no non-finite score ever leaves retrieval.
    finite = [r for r in raw if math.isfinite(r.similarity)]
    if len(finite) != len(raw):
        logger.warning("dropped retrieved chunks with a non-finite score", extra={"dropped": len(raw) - len(finite)})
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
        for r in finite
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