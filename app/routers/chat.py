"""
app/routers/chat.py — POST /chat endpoint
==========================================
This router handles the core product feature: sending a message to the AI
and getting a reply, with conversation history persisted to Postgres.

The route function is intentionally thin — it orchestrates the steps but
delegates all logic to the service layer:
  - chat_history.py: DB reads and writes
  - ai.py:           Ollama call

Why keep the router thin?
  A route function that directly queries the DB, calls Ollama, and formats
  the response is hard to test and hard to reuse. If any step needs changing
  (e.g. history limit, token storage format), you hunt through interleaved
  HTTP and business logic. The thin router pattern keeps each layer focused.

Request/response flow for POST /chat
  1. FastAPI validates the JSON body against ChatRequest (Pydantic)
     → 422 automatically if validation fails
  2. conversation_id is None → create a new conversation row in Postgres
     conversation_id is set → load that conversation's history
  3. Call ai.py → get reply from Ollama
  4. Save user message and assistant reply to Postgres
  5. Return ChatResponse

Concurrency note
  Every step uses `await` — no step blocks the event loop. While Ollama is
  thinking (which can take seconds for a large model), other requests are
  handled normally.
"""

import uuid

import structlog
from fastapi import APIRouter, HTTPException, status

from app.dependencies import DB, Redis
from app.models.database import MessageRole
from app.models.schemas import (
    AIServiceError,
    ChatRequest,
    ChatResponse,
    DocsChatRequest,
    DocsChatResponse,
)
from app.services import ai as ai_service
from app.services import chat_history
from app.services import documents as docs_service
from app.services import rag as rag_service

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Send a message and get an AI reply",
    description=(
        "Send a message to the AI. Optionally provide a `conversation_id` "
        "to continue an existing conversation — omit it to start a new one. "
        "The response always includes the `conversation_id` to use in follow-up requests."
    ),
    status_code=status.HTTP_200_OK,
)
async def chat(
    request: ChatRequest,
    db: DB,  # AsyncSession injected by FastAPI via Depends(get_db) — see dependencies.py
    redis_client: Redis,  # shared Redis pool — used for the AI response cache
) -> ChatResponse:
    """
    Core chat endpoint: persist history, call Ollama, return the reply.

    Why we save the user message AFTER the AI call (not before):
      If we saved the user message first and then the AI call failed, the user
      message would be in the DB but with no reply — orphaned data. By saving
      both messages in sequence after a successful AI response, we keep the DB
      consistent: either both are saved, or neither is (the transaction rolls
      back on exception).
    """

    # -----------------------------------------------------------------------
    # Step 1: Resolve conversation
    # -----------------------------------------------------------------------
    # If the client provided a conversation_id, use it.
    # If not, create a new conversation row in Postgres.
    if request.conversation_id is None:
        conversation = await chat_history.create_conversation(db)
        conversation_id = conversation.id
        await logger.ainfo(
            "new conversation started",
            conversation_id=str(conversation_id),
        )
    else:
        # Parse the string ID from the request into a UUID object.
        # Raises ValueError if the string is not a valid UUID — caught below.
        try:
            conversation_id = uuid.UUID(request.conversation_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="conversation_id must be a valid UUID",
            )

    # -----------------------------------------------------------------------
    # Step 2: Load conversation history
    # -----------------------------------------------------------------------
    # Returns up to 20 messages, oldest first (chronological order).
    # For a brand-new conversation this is an empty list — that's fine,
    # the AI call just has no prior context.
    history = await chat_history.get_history(db, conversation_id)

    # -----------------------------------------------------------------------
    # Step 3: Call Ollama
    # -----------------------------------------------------------------------
    try:
        ai_response = await ai_service.get_ai_reply(
            history=history,
            new_user_message=request.message,
            conversation_id=str(conversation_id),
            redis_client=redis_client,
        )
    except AIServiceError as exc:
        # The AI service is down or the model is unavailable.
        # Return 503 Service Unavailable — this is the correct status code
        # when a dependency your service needs is temporarily unavailable.
        # 500 would imply a bug in our code; 503 tells clients to retry later.
        await logger.aerror(
            "ai service failed",
            conversation_id=str(conversation_id),
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI service is currently unavailable. Please try again shortly.",
        ) from exc

    # -----------------------------------------------------------------------
    # Step 4: Persist both messages
    # -----------------------------------------------------------------------
    # Save user message first (chronological order), then the assistant reply.
    # Both use the same DB session / transaction — if either flush fails, the
    # transaction rolls back and neither message is committed to Postgres.
    await chat_history.add_message(
        db=db,
        conversation_id=conversation_id,
        role=MessageRole.USER,
        content=request.message,
        tokens_used=None,  # user messages don't consume output tokens
    )

    await chat_history.add_message(
        db=db,
        conversation_id=conversation_id,
        role=MessageRole.ASSISTANT,
        content=ai_response.reply,
        tokens_used=ai_response.tokens_used,  # output tokens from Ollama
    )

    # Commit the transaction — both messages are now durable in Postgres.
    # The get_db() dependency normally commits on clean exit, but calling it
    # explicitly here makes the intent clear: we want both messages committed
    # together as a unit before we return the response.
    await db.commit()

    # -----------------------------------------------------------------------
    # Step 5: Return response
    # -----------------------------------------------------------------------
    return ChatResponse(
        conversation_id=str(conversation_id),
        reply=ai_response.reply,
        tokens_used=ai_response.total_tokens,  # total (input + output) for the client
        model=ai_response.model,
        cache_hit=ai_response.cache_hit,
    )


# ---------------------------------------------------------------------------
# POST /chat/docs — ask a question grounded in uploaded documents
# ---------------------------------------------------------------------------
# This endpoint is independent of the plain /chat flow:
#   - No conversation history (each question is standalone).
#   - No Redis cache (RLM responses are non-deterministic and cache-hostile).
#   - No persistent storage of questions/answers (kept stateless for v1; can
#     add later if we want per-user history of doc questions).
#
# Errors map as:
#   422  — invalid UUID in document_ids
#   404  — every document_id was missing (nothing to query)
#   503  — dspy/LiteLLM/Ollama call failed (same status as /chat for AI
#          provider outages — keeps SLO math consistent across endpoints)


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
