"""
Knowledge retrieval tool — Passage 4 §3.5 (G05): "RAG query against
knowledge_chunks / embeddings (P1 §7)."

Registered in Phase 2 as a real contract with a "not yet implemented"
handler, exactly as that phase's own docstring said it would be:
"once Phase 3 lands, only this handler's body changes — the name,
schema, and governance a provider already knows about stay identical."
This is that change — the handler now calls `rag.pipeline.retrieve`,
the same function `routers/knowledge.py`'s search endpoint and
`routers/analyze.py`'s knowledge-context enrichment both call, so all
three retrieval call sites stay in sync by construction rather than by
convention.
"""

from __future__ import annotations

from ..db.client import is_db_configured
from ..rag.pipeline import retrieve
from .schemas import ToolContext, ToolDefinition, ToolResult


async def _knowledge_retrieval_handler(input_data: dict, context: ToolContext) -> ToolResult:
    query = input_data["query"]
    if not is_db_configured():
        return ToolResult(
            output={"query": query, "results": []},
            available=False,
            unavailable_reason="No database is configured — there is no knowledge base to query.",
        )

    top_k = int(input_data.get("topK", 5))
    results = await retrieve(query, top_k=top_k)
    if not results:
        return ToolResult(
            output={"query": query, "results": []},
            available=False,
            unavailable_reason=(
                "No knowledge has been ingested yet — upload a document via "
                "POST /api/knowledge/upload first."
            ),
        )
    return ToolResult(output={"query": query, "results": results})


KNOWLEDGE_RETRIEVAL_TOOL = ToolDefinition(
    name="knowledge_retrieval",
    description="RAG query against ingested knowledge documents, returning labelled, ranked chunks.",
    input_schema={
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 2000},
            "topK": {"type": "integer", "minimum": 1, "maximum": 25},
        },
    },
    output_schema={
        "type": "object",
        "required": ["query", "results"],
        "properties": {"query": {"type": "string"}, "results": {"type": "array"}},
    },
    handler=_knowledge_retrieval_handler,
    kind="read_only",
    timeout_seconds=15.0,  # embedding + vector search can run longer than the 10s default
)