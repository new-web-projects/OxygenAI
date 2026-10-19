"""
Embedding providers for the RAG pipeline — Passage 1 §7's EMBEDDING
stage.

Two real implementations, chosen automatically:

* `HashingEmbeddingProvider` — the default, always available, zero
  external dependency. Uses the classic feature-hashing ("hashing
  trick") technique: each word is hashed into one of 1536 buckets and
  the bucket is incremented (sign-hashed to reduce collision bias),
  then the vector is L2-normalized so cosine similarity behaves
  sensibly. This is a real, working information-retrieval technique —
  two chunks that share vocabulary get measurably higher similarity
  than two that don't — but it is lexical, not semantic: it cannot
  tell that "equity" and "stock" mean similar things the way a trained
  neural embedding model can. Disclosed honestly rather than presented
  as equivalent to a real embedding model.

* `GoogleEmbeddingProvider` — real code against Google's actual current
  API (`gemini-embedding-001` via `client.aio.models.embed_content`,
  `output_dimensionality=1536`, confirmed against Google's current
  documentation — not assumed from training data). Used automatically
  when `GOOGLE_AI_API_KEY` is set (the same credential already used for
  Gemma's hosted transport — no new credential to configure). This is
  implementation-complete but has NOT been live-verified in this
  session, since no real key is available here: it is code-complete and
  integration-complete, with live verification blocked by the missing
  external credential, per this project's own reporting standard.

Choosing an embedding model is strictly about which vector represents a
knowledge chunk internally — it has nothing to do with the three named
AI *providers* (Custom AI, Grok, Gemma 4) a user selects for trading
analysis, and does not introduce a "Gemini provider" in that sense.
`gemini-embedding-001` is simply Google's name for their current
embedding model, used here as an internal utility.
"""

from __future__ import annotations

import hashlib
import math
import os
from typing import Protocol

from ..observability import get_logger
from .tokenize import tokenize

logger = get_logger("rag.embeddings")

EMBEDDING_DIM = 1536  # fixed by the embeddings.vector(1536) column


class EmbeddingProvider(Protocol):
    model_name: str

    async def embed(self, text: str) -> list[float]: ...
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbeddingProvider:
    """
    Always-available, dependency-free, deterministic default. See the
    module docstring for what this can and cannot do.

    Contract worth knowing: text with no tokens at all (only punctuation,
    emoji, whitespace) has no direction, so `embed` returns the all-zero
    vector for it. That is an honest "no signal", NOT a storable embedding
    — cosine similarity against it is undefined (pgvector returns NaN).
    Callers must check `db.knowledge.is_usable_vector` before storing or
    searching with a vector; `rag.pipeline` does, for ingestion and for
    queries. Tokenization is Unicode-aware (`rag/tokenize.py`) so scripts
    such as Bengali and Hindi are NOT "no signal"; ASCII text tokenizes
    exactly as it always did, so existing stored vectors are unchanged.
    """

    model_name = "hashing-trick-v1"

    def _hash_bucket(self, word: str) -> tuple[int, float]:
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
        # A second, independent bit of the same hash decides the sign —
        # this is what keeps unrelated words from all pushing the vector
        # in the same direction, which would make every embedding look
        # artificially similar regardless of content.
        sign = 1.0 if digest[4] & 1 else -1.0
        return bucket, sign

    async def embed(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIM
        words = tokenize(text)
        if not words:
            return vector
        for word in words:
            bucket, sign = self._hash_bucket(word)
            vector[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0:
            return vector
        return [v / norm for v in vector]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]


class GoogleEmbeddingProvider:
    """
    Real code against Google's current embedding API. NOT live-verified
    in this session — no GOOGLE_AI_API_KEY is available here. Code-
    complete and integration-complete; live verification is blocked by
    the missing external credential.
    """

    model_name = "gemini-embedding-001"

    def __init__(self) -> None:
        self._api_key = os.environ.get("GOOGLE_AI_API_KEY", "").strip()

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def embed(self, text: str) -> list[float]:
        result = await self.embed_batch([text])
        return result[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not self._api_key:
            raise RuntimeError("GOOGLE_AI_API_KEY is not set")
        from google import genai  # type: ignore[import-not-found]
        from google.genai.types import EmbedContentConfig  # type: ignore[import-not-found]

        client = genai.Client(api_key=self._api_key)
        response = await client.aio.models.embed_content(
            model=self.model_name,
            contents=texts,
            config=EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT", output_dimensionality=EMBEDDING_DIM
            ),
        )
        return [list(e.values) for e in response.embeddings]


def get_embedding_provider() -> EmbeddingProvider:
    """
    Google when configured, the hashing fallback otherwise — the same
    graceful-degradation shape used for the native layer, market data,
    and every AI provider elsewhere in this codebase.
    """
    google = GoogleEmbeddingProvider()
    if google.is_configured():
        return google
    logger.info("GOOGLE_AI_API_KEY not set; using the hashing-trick embedding fallback")
    return HashingEmbeddingProvider()