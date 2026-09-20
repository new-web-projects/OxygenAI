"""
Knowledge retrieval tool — Passage 4 §3.5 (G05): "RAG query against
knowledge_chunks / embeddings (P1 §7)."

The full RAG pipeline (ingest -> chunk -> embed -> store -> retrieve ->
rerank) is Phase 3 in the implementation plan and does not exist yet.
Registering this tool now, with its real contract, rather than waiting
for Phase 3 to also add the tool means: (a) the 15-tool registry is
complete in this phase, matching G05's own count, and (b) once Phase 3
lands, only this handler's body changes — the name, schema, and
governance a provider already knows about stay identical.

The table check below (are there any rows in knowledge_chunks at all)
is deliberately the only thing this handler does — it does not attempt
a partial/fake retrieval over an empty table, which would be
indistinguishable from "your query matched nothing" and would hide the
real reason (no ingestion pipeline exists yet) from whoever is reading
the tool's output.
"""

from __future__ import annotations

from ..db.client import get_pool, is_db_configured
from .schemas import ToolContext, ToolDefinition, ToolResult


async def _knowledge_retrieval_handler(input_data: dict, context: ToolContext) -> ToolResult:
    if not is_db_configured():
        return ToolResult(
            output={"query": input_data["query"], "results": []},
            available=False,
            unavailable_reason="No database is configured — there is no knowledge base to query.",
        )
    pool = await get_pool()
    has_any = await pool.fetchval("SELECT 1 FROM knowledge_chunks LIMIT 1")
    if not has_any:
        return ToolResult(
            output={"query": input_data["query"], "results": []},
            available=False,
            unavailable_reason=(
                "No knowledge has been ingested yet — the RAG ingestion pipeline "
                "(Passage 1 §7) is not yet implemented."
            ),
        )
    # Reachable once Phase 3's ingestion pipeline has written rows;
    # left as an explicit not-yet-implemented path rather than a partial
    # retrieval implementation, since a real pgvector similarity query
    # needs the same embedding model Phase 3 chooses for ingestion.
    return ToolResult(
        output={"query": input_data["query"], "results": []},
        available=False,
        unavailable_reason="Retrieval is not yet implemented even though chunks exist — Phase 3 work in progress.",
    )


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
    availability_note="Not yet available — RAG ingestion pipeline (Passage 1 §7) is a later implementation phase.",
)