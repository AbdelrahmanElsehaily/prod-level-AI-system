"""
app/routers/documents.py — document upload / list / delete
===========================================================
Three endpoints for managing the corpus that dspy.RLM can query.

  POST   /documents       multipart upload, returns the new document metadata
  GET    /documents       list documents (newest first), no content_text
  DELETE /documents/{id}  delete one document by UUID

The router is intentionally thin. All file-format handling, size checks,
and DB writes live in app/services/documents.py. The router's job is:
  1. Validate inputs from the HTTP layer (multipart, UUIDs).
  2. Map service exceptions to HTTP status codes.
  3. Serialise ORM objects to the response schema.
"""

import uuid

import structlog
from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.dependencies import DB
from app.models.schemas import DocumentResponse
from app.services import documents as docs_service

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["Documents"])


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document for the RLM to query",
)
async def upload_document(
    db: DB,
    file: UploadFile = File(..., description="PDF, .txt, or .md file to upload."),
) -> DocumentResponse:
    """
    Upload a PDF/TXT/MD file. The text is extracted and stored; the
    original binary is discarded.

    Returns 201 with the document metadata on success.
    Returns 413 if the file or corpus is over the size cap.
    Returns 415 if the file type is unsupported.
    """
    # UploadFile.read() loads the whole file into memory. That's fine here
    # because the size cap (1 MiB per file) is small and enforced by
    # save_document() before we do anything expensive.
    raw = await file.read()

    try:
        doc = await docs_service.save_document(
            db,
            filename=file.filename or "unnamed",
            content_type=file.content_type,
            raw=raw,
        )
    except docs_service.DocumentTooLargeError as exc:
        # 413 Payload Too Large is the precise status for "your upload
        # exceeds the size we allow". 400 would also be acceptable but
        # less informative.
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except docs_service.UnsupportedDocumentTypeError as exc:
        # 415 Unsupported Media Type — there is a real spec match for
        # "I know what you sent, I just don't process that format".
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc

    # from_attributes=True on the schema means this works directly from
    # the ORM object — no manual field copying.
    # str(doc.id) because the schema declares id as str (UUIDs serialise
    # cleanly to JSON but Pydantic v2 requires the explicit conversion).
    return DocumentResponse(
        id=str(doc.id),
        filename=doc.filename,
        content_type=doc.content_type,
        size_bytes=doc.size_bytes,
        created_at=doc.created_at,
    )


@router.get(
    "",
    response_model=list[DocumentResponse],
    summary="List uploaded documents",
)
async def list_documents(db: DB) -> list[DocumentResponse]:
    """Return all documents, newest first. content_text is NOT returned."""
    docs = await docs_service.list_documents(db)
    return [
        DocumentResponse(
            id=str(doc.id),
            filename=doc.filename,
            content_type=doc.content_type,
            size_bytes=doc.size_bytes,
            created_at=doc.created_at,
        )
        for doc in docs
    ]


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document",
)
async def delete_document(document_id: str, db: DB) -> None:
    """
    Delete a document by UUID.

    Returns 204 on success (no body).
    Returns 404 if the document does not exist.
    Returns 422 if the path parameter is not a valid UUID.
    """
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="document_id must be a valid UUID",
        ) from exc

    try:
        await docs_service.delete_document(db, doc_uuid)
    except docs_service.DocumentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
