"""
Reranking - Passage 1 section 7's "RERANKING (cross-encoder or
LLM-based)" stage.

A genuine cross-encoder needs a trained model this environment has no
reliable way to fetch and run, and an LLM-based reranker needs a live
provider call this session cannot verify without credentials. Rather
than fake either, this implements a real, deterministic third option in
the same family the blueprint's own phrasing leaves room for ("or"):
lexical term-overlap reranking, blended with the vector similarity score
retrieval already produced. It is a legitimate, classical
information-retrieval technique - not a stand-in pretending to be a
cross-encoder - and it composes cleanly with either the hashing or the
Google embedding provider, since it operates on the retrieved text
directly rather than on embedding internals.

Swapping in a cross-encoder or an LLM-based reranker later only means
replacing rerank()'s body; the function signature stays the same - take
the query and the vector-search results, return them reordered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .tokenize import tokenize

_LEXICAL_WEIGHT = 0.35


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    document_id: str
    document_title: str
    source_type: str
    content: str
    position: int
    vector_score: float


def _term_set(text: str) -> set[str]:
    return set(tokenize(text))


def _lexical_overlap(query_terms: set[str], chunk_terms: set[str]) -> float:
    if not query_terms or not chunk_terms:
        return 0.0
    overlap = len(query_terms & chunk_terms)
    return overlap / len(query_terms)


def rerank(query: str, results: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """
    Returns results reordered by a blended score. Does not mutate
    vector_score - that field stays the original similarity value; the
    blended score only determines order.

    The sort key is always a real number. A NaN key makes every comparison
    False, which leaves a sort in an unspecified order — before this guard
    a single NaN-scored chunk was left wherever it happened to sit
    (including first) and could scramble its neighbours. Retrieval no
    longer passes non-finite scores in (`rag.pipeline.retrieve` drops
    them), so this is the last line of defence: a non-finite score ranks
    below every real one instead of poisoning the sort.
    """
    query_terms = _term_set(query)
    if not results:
        return results

    def blended(chunk: RetrievedChunk) -> float:
        if not math.isfinite(chunk.vector_score):
            return -math.inf
        lexical = _lexical_overlap(query_terms, _term_set(chunk.content))
        return (1 - _LEXICAL_WEIGHT) * chunk.vector_score + _LEXICAL_WEIGHT * lexical

    return sorted(results, key=blended, reverse=True)