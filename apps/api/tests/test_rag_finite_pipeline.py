"""
Reranker and retrieval/ingestion pipeline behaviour with degenerate numbers
(no database — the embedder and the SQL layer are replaced with doubles, so
every scenario is exact and repeatable).

The headline guarantee: no non-finite score ever leaves `retrieve()`, which
is what the search endpoint, the analysis prompt and the knowledge tool all
call.
"""

from __future__ import annotations

import json
import math

import pytest

import app.rag.pipeline as pipeline
from app.db.knowledge import SearchRow
from app.rag.chunking import Chunk
from app.rag.rerank import RetrievedChunk, rerank

from .strict_json import assert_all_finite

NAN = float("nan")
UNIT = [1.0] + [0.0] * 1535
ZERO = [0.0] * 1536


def _rc(chunk_id: str, score: float, content: str = "rsi momentum note") -> RetrievedChunk:
    return RetrievedChunk(chunk_id, "doc", "title", "knowledge", content, 0, score)


def _row(chunk_id: str, similarity: float, content: str = "rsi momentum note") -> SearchRow:
    return SearchRow(chunk_id, "doc", "title", "knowledge", content, 0, similarity)


# ---------------------------------------------------------------- rerank


@pytest.mark.parametrize("nan_position", [0, 1, 2, 3, 4])
def test_a_nan_score_never_disturbs_the_order_of_finite_chunks(nan_position):
    finite = [_rc("c1", 0.3), _rc("c2", 0.9), _rc("c3", 0.6), _rc("c4", 0.1)]  # deliberately unsorted
    mixed = list(finite)
    mixed.insert(nan_position, _rc("bad", NAN))

    ids = [c.chunk_id for c in rerank("rsi momentum", mixed)]

    # Finite chunks in true descending order; the non-finite one ranks last.
    assert ids == ["c2", "c3", "c1", "c4", "bad"]


def test_infinite_scores_are_treated_the_same_way():
    ids = [c.chunk_id for c in rerank("q", [_rc("inf", math.inf), _rc("a", 0.2), _rc("ninf", -math.inf), _rc("b", 0.8)])]
    assert ids[:2] == ["b", "a"]
    assert set(ids[2:]) == {"inf", "ninf"}


def test_rerank_does_not_mutate_the_reported_score():
    original = [_rc("a", 0.4), _rc("b", 0.8)]
    ranked = rerank("rsi momentum", original)
    assert {c.chunk_id: c.vector_score for c in ranked} == {"a": 0.4, "b": 0.8}


def test_rerank_is_deterministic():
    items = [_rc(f"c{i}", 0.5) for i in range(6)]
    runs = {tuple(c.chunk_id for c in rerank("rsi momentum", list(items))) for _ in range(5)}
    assert len(runs) == 1


def test_lexical_overlap_now_works_for_non_ascii_queries():
    # Equal vector scores: only the lexical term can separate them. With the
    # old ASCII-only tokenizer both overlaps were 0 and the order was arbitrary.
    related = _rc("related", 0.5, content="ভারতীয় শেয়ার বাজার বিশ্লেষণ")
    unrelated = _rc("unrelated", 0.5, content="completely different english text")
    assert [c.chunk_id for c in rerank("শেয়ার বাজার", [unrelated, related])] == ["related", "unrelated"]


# ---------------------------------------------------------------- retrieve


class _FakeEmbedder:
    model_name = "fake-embedder"

    def __init__(self, vector):
        self._vector = vector

    async def embed(self, text):
        return list(self._vector)

    async def embed_batch(self, texts):
        return [list(self._vector) for _ in texts]


@pytest.fixture
def retrieval(monkeypatch):
    """Install a fake embedder and a fake SQL layer; returns a handle to both."""

    class Handle:
        rows: list[SearchRow] = []
        searched = 0

    async def fake_search(vector, top_k):
        Handle.searched += 1
        return list(Handle.rows)

    monkeypatch.setattr(pipeline, "similarity_search", fake_search)
    monkeypatch.setattr(pipeline, "get_embedding_provider", lambda: _FakeEmbedder(UNIT))
    Handle.rows, Handle.searched = [], 0
    return Handle


@pytest.mark.asyncio
async def test_non_finite_rows_never_leave_retrieve(retrieval):
    retrieval.rows = [_row("a", 0.4), _row("nan", NAN), _row("b", 0.9), _row("inf", math.inf)]

    results = await pipeline.retrieve("rsi momentum", top_k=5)

    assert [r["chunkId"] for r in results] == ["b", "a"]
    assert_all_finite(results)
    json.dumps(results, allow_nan=False)  # exactly what the JSON encoder requires


@pytest.mark.asyncio
async def test_a_query_with_no_usable_direction_returns_nothing_without_searching(monkeypatch, retrieval):
    monkeypatch.setattr(pipeline, "get_embedding_provider", lambda: _FakeEmbedder(ZERO))
    retrieval.rows = [_row("a", 0.9)]

    assert await pipeline.retrieve("???", top_k=5) == []
    assert retrieval.searched == 0  # never reached the database


@pytest.mark.asyncio
async def test_retrieve_with_the_real_embedder_and_a_tokenless_query_returns_nothing(monkeypatch, retrieval):
    from app.rag.embeddings import HashingEmbeddingProvider

    monkeypatch.setattr(pipeline, "get_embedding_provider", lambda: HashingEmbeddingProvider())
    retrieval.rows = [_row("a", 0.9)]
    for query in ["???", "   ", "😀", "--- !!!"]:
        assert await pipeline.retrieve(query, top_k=5) == []
    assert retrieval.searched == 0


@pytest.mark.asyncio
async def test_all_rows_non_finite_yields_an_empty_list_not_an_error(retrieval):
    retrieval.rows = [_row("x", NAN), _row("y", NAN)]
    assert await pipeline.retrieve("rsi", top_k=3) == []


# ---------------------------------------------------------------- ingest


@pytest.fixture
def ingestion(monkeypatch):
    """Replace chunking, embedding and every DB write; record what happens."""

    class Handle:
        statuses: list[str] = []
        stored: list[tuple[list[str], list[list[float]]]] = []
        chunks: list[Chunk] = []
        vectors: list[list[float]] = []

    async def create_document(title, source_type, uploaded_by):
        return "doc-1"

    async def mark_status(document_id, status):
        Handle.statuses.append(status)

    async def insert(document_id, contents, vectors, model):
        Handle.stored.append((contents, vectors))

    class Embedder:
        model_name = "fake-embedder"

        async def embed_batch(self, texts):
            return list(Handle.vectors)

    monkeypatch.setattr(pipeline, "create_document", create_document)
    monkeypatch.setattr(pipeline, "mark_document_status", mark_status)
    monkeypatch.setattr(pipeline, "insert_chunks_with_embeddings", insert)
    monkeypatch.setattr(pipeline, "chunk_text", lambda cleaned: list(Handle.chunks))
    monkeypatch.setattr(pipeline, "get_embedding_provider", lambda: Embedder())
    Handle.statuses, Handle.stored, Handle.chunks, Handle.vectors = [], [], [], []
    return Handle


@pytest.mark.asyncio
async def test_unembeddable_chunks_are_skipped_and_the_rest_are_stored(ingestion):
    ingestion.chunks = [Chunk("real text", 0), Chunk("?!", 1), Chunk("more real text", 2)]
    ingestion.vectors = [UNIT, ZERO, UNIT]

    result = await pipeline.ingest_text("t", "body", "knowledge")

    assert (result.chunk_count, result.skipped_chunks) == (2, 1)
    assert ingestion.stored == [(["real text", "more real text"], [UNIT, UNIT])]
    assert ingestion.statuses == ["ready"]


@pytest.mark.asyncio
async def test_a_document_with_nothing_embeddable_is_rejected_and_marked_failed(ingestion):
    ingestion.chunks = [Chunk("?!", 0), Chunk("😀", 1)]
    ingestion.vectors = [ZERO, ZERO]

    with pytest.raises(pipeline.NoIndexableContentError, match="could be indexed"):
        await pipeline.ingest_text("t", "body", "knowledge")

    assert ingestion.stored == []  # nothing poisonous was written
    assert ingestion.statuses == ["failed"]


@pytest.mark.asyncio
async def test_an_empty_document_is_the_same_clean_error_and_still_a_value_error(ingestion):
    ingestion.chunks = []
    with pytest.raises(ValueError) as caught:  # existing `except ValueError` callers keep working
        await pipeline.ingest_text("t", "   ", "knowledge")
    assert isinstance(caught.value, pipeline.NoIndexableContentError)
    assert ingestion.statuses == ["failed"]


@pytest.mark.asyncio
async def test_a_short_provider_response_is_an_error_not_silent_data_loss(ingestion):
    ingestion.chunks = [Chunk("a", 0), Chunk("b", 1)]
    ingestion.vectors = [UNIT]  # one vector for two chunks; zip() would have dropped a chunk silently

    with pytest.raises(ValueError, match="1 vectors for 2 chunks"):
        await pipeline.ingest_text("t", "body", "knowledge")

    assert ingestion.stored == []
    assert ingestion.statuses == ["failed"]
