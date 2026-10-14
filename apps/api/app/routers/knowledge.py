"""
Knowledge endpoints - the "Knowledge" category Passage 4 section 6.3
(G13) names among the 19 required endpoint categories.

Authorization (Passage 1 §8: knowledge memory is retained "until admin
deletes" — i.e. it is an admin-curated store):

* Upload (text and file) and delete require the `admin` role. Every
  document feeds EVERY user's future analysis prompts, so letting any
  self-registered account add or remove documents would let one user
  poison, or erase, the context all others reason over.
* Search and list require authentication (any role): they are read-only,
  but search spends embedding-provider quota and the document list is
  not public information.

`routers/analyze.py` calls the same underlying `rag.pipeline.retrieve`
function internally on behalf of the already-authenticated caller.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile

from ..errors import NotFoundError, ValidationError
from ..rag.chunking import UnsupportedFileTypeError, extract_text
from ..rag.pipeline import ingest_text, retrieve
from ..schemas import KnowledgeChunkResponse, KnowledgeSearchRequest, KnowledgeUploadRequest
from ..security.dependencies import AuthenticatedUser, get_current_user, require_role

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

# Passage 1 section 7's upload diagram names PDF/TXT/CSV specifically;
# capped well above any real research note or filing to keep ingestion
# bounded rather than accepting an arbitrarily large upload.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.post("/upload")
async def upload_text(
    body: KnowledgeUploadRequest, user: AuthenticatedUser = Depends(require_role("admin"))
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
    user: AuthenticatedUser = Depends(require_role("admin")),
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
async def search(
    body: KnowledgeSearchRequest, user: AuthenticatedUser = Depends(get_current_user)
) -> list[KnowledgeChunkResponse]:
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
async def list_knowledge_documents(
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[dict]:
    from ..db.knowledge import list_documents

    return await list_documents()


@router.delete("/documents/{document_id}")
async def delete_knowledge_document(
    document_id: str, user: AuthenticatedUser = Depends(require_role("admin"))
) -> dict:
    from ..db.knowledge import delete_document

    deleted = await delete_document(document_id)
    if not deleted:
        raise NotFoundError(f"No knowledge document with id '{document_id}'")
    return {"ok": True}