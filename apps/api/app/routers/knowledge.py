"""
Knowledge endpoints - the "Knowledge" category Passage 4 section 6.3
(G13) names among the 19 required endpoint categories.

Upload requires authentication (any authenticated user, attributed via
`uploaded_by`) since it's a mutating, shared-resource action - every
uploaded document feeds every future analysis's RAG context, not just
the uploader's own. Search stays open: it's read-only, and
`routers/analyze.py` calls the same underlying `rag.pipeline.retrieve`
function internally regardless of whether the request itself is
authenticated.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile

from ..errors import NotFoundError, ValidationError
from ..rag.chunking import UnsupportedFileTypeError, extract_text
from ..rag.pipeline import ingest_text, retrieve
from ..schemas import KnowledgeChunkResponse, KnowledgeSearchRequest, KnowledgeUploadRequest
from ..security.dependencies import AuthenticatedUser, get_current_user

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

# Passage 1 section 7's upload diagram names PDF/TXT/CSV specifically;
# capped well above any real research note or filing to keep ingestion
# bounded rather than accepting an arbitrarily large upload.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.post("/upload")
async def upload_text(
    body: KnowledgeUploadRequest, user: AuthenticatedUser = Depends(get_current_user)
) -> dict:
    result = await ingest_text(body.title, body.content, body.sourceType, uploaded_by=user.id)
    return {
        "documentId": result.document_id,
        "chunkCount": result.chunk_count,
        "embeddingModel": result.embedding_model,
    }


@router.post("/upload/file")
async def upload_file(
    title: str = Form(...),
    sourceType: str = Form("knowledge"),
    file: UploadFile = File(...),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValidationError(f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit")

    try:
        text = extract_text(raw, file.content_type or "", file.filename or "")
    except UnsupportedFileTypeError as err:
        raise ValidationError(str(err)) from err

    result = await ingest_text(title, text, sourceType, uploaded_by=user.id)
    return {
        "documentId": result.document_id,
        "chunkCount": result.chunk_count,
        "embeddingModel": result.embedding_model,
    }


@router.post("/search")
async def search(body: KnowledgeSearchRequest) -> list[KnowledgeChunkResponse]:
    hits = await retrieve(body.query, top_k=body.topK)
    return [
        KnowledgeChunkResponse(
            chunkId=h["chunkId"],
            documentId=h["documentId"],
            documentTitle=h["documentTitle"],
            sourceType=h["sourceType"],
            content=h["content"],
            score=h["score"],
            position=h["position"],
        )
        for h in hits
    ]


@router.get("/documents")
async def list_knowledge_documents() -> list[dict]:
    from ..db.knowledge import list_documents

    return await list_documents()


@router.delete("/documents/{document_id}")
async def delete_knowledge_document(
    document_id: str, user: AuthenticatedUser = Depends(get_current_user)
) -> dict:
    from ..db.knowledge import delete_document

    deleted = await delete_document(document_id)
    if not deleted:
        raise NotFoundError(f"No knowledge document with id '{document_id}'")
    return {"ok": True}