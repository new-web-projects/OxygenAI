"""
RAG / Knowledge System - Passage 1 section 7:

  UPLOAD (PDF/TXT/CSV) -> TEXT EXTRACTION -> CLEANING -> CHUNKING
  -> EMBEDDING -> pgvector STORAGE -> METADATA STORAGE -> RETRIEVAL
  -> RERANKING -> LABELED AI CONTEXT

- chunking.py   - text extraction (PDF via pypdf, CSV, TXT), cleaning,
                   sentence-boundary-aware chunking with overlap.
- embeddings.py - EmbeddingProvider abstraction: a real, dependency-free
                   hashing-trick default, and real (code-complete,
                   not live-verified without a key) Google embedding
                   code.
- rerank.py     - a real lexical-overlap reranker, honestly
                   distinguished from a cross-encoder.
- pipeline.py   - orchestrates ingest_text() and retrieve(), the two
                   entry points every caller (the knowledge router,
                   routers/analyze.py, the knowledge_retrieval tool)
                   uses.
"""