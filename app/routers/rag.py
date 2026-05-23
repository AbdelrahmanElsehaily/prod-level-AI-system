"""
app/routers/rag.py — document Q&A endpoint (dspy.RLM)
======================================================
Owns the single endpoint that turns a question + uploaded documents into
an answer, using app/services/rag.py's dspy.RLM wrapper.

Why a separate router from chat.py?
  /chat and /chat/docs share a URL prefix but very little behaviour:
    - /chat        keeps conversation history, uses Redis cache, calls Ollama directly
    - /chat/docs   stateless, no cache (RLM responses diverge), runs dspy.RLM
  Lumping them in one file made chat.py grow imports for documents +
  rag services it didn't otherwise need. One router per coherent feature
  is the rule that keeps these files small.

Error mapping
-------------
  422  — invalid UUID in document_ids
  404  — no documents matched (either every requested ID was missing,
         or the corpus is empty)
  503  — dspy / LiteLLM / Ollama / Pyodide call failed (same status the
         plain /chat endpoint uses for AI-provider outages, so the SLO
         dashboard counts both as "AI layer down")
"""

import uuid

import structlog
from fastapi import APIRouter, HTTPException, status

from app.dependencies import DB
from app.models.schemas import DocsChatRequest, DocsChatResponse
from app.services import documents as docs_service
from app.services import rag as rag_service

logger = structlog.get_logger(__name__)

# We mount /chat/docs as a fully-qualified path rather than using
# prefix="/chat" — there is only one route here, and a prefix would
# imply we plan to add /chat/* siblings in this file (we don't; those
# belong in chat.py).
router = APIRouter(tags=["Document Q&A"])


@router.post(
    "/chat/docs",
    response_model=DocsChatResponse,
    summary="Ask a question about uploaded documents (dspy.RLM)",
    description=(
        "Runs the question through dspy.RLM, which loads the selected "
        "documents into a Pyodide sandbox and lets the LLM recursively "
        "navigate them. Omit document_ids to use all uploaded documents."
    ),
)
async def chat_docs(
    request: DocsChatRequest,
    db: DB,
) -> DocsChatResponse:
    """RLM Q&A over stored documents."""
    # --- Step 1: Load documents into memory ---
    if request.document_ids:
        # Parse + validate UUIDs up-front. Doing it here (not inside the
        # service) keeps the service free of HTTP error codes.
        try:
            doc_uuids = [uuid.UUID(did) for did in request.document_ids]
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="document_ids must all be valid UUIDs",
            ) from exc
        docs = await docs_service.get_documents_by_ids(db, doc_uuids)
    else:
        docs = await docs_service.get_all_documents_with_content(db)

    if not docs:
        # If the caller asked for specific IDs and none exist, OR the corpus
        # is empty, there is literally nothing to feed the RLM. 404 communicates
        # "the resource you're asking about doesn't exist" more accurately
        # than 400 "you sent a bad request".
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No documents found. Upload one with POST /documents first, "
                "or check that the document_ids you provided are correct."
            ),
        )

    # --- Step 2: Run the RLM ---
    try:
        answer = await rag_service.ask_documents(
            question=request.question,
            documents=docs,
        )
    except Exception as exc:
        # Same error policy as plain /chat: anything that comes from the
        # AI layer (DSPy, LiteLLM, Ollama, sandbox errors) becomes 503.
        # We log first so the original stack is preserved in structlog
        # before Sentry captures it.
        await logger.aerror(
            "rlm service failed",
            error=str(exc),
            documents_used=[str(d.id) for d in docs],
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document Q&A service is currently unavailable. Please try again shortly.",
        ) from exc

    return DocsChatResponse(
        answer=answer.answer,
        documents_used=[str(d.id) for d in docs],
        iterations=answer.iterations,
        total_tokens=answer.total_tokens,
    )
