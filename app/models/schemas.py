"""
app/models/schemas.py — Pydantic HTTP request/response schemas
===============================================================
These models define the shape of data coming IN and going OUT of the API
over HTTP. They are completely separate from the SQLAlchemy ORM models in
database.py, which define how data is stored in Postgres.

Why keep them separate?
  ORM models care about: column types, foreign keys, indexes, nullability.
  HTTP schemas care about: validation rules, field documentation, serialisation.

  If you use the same class for both, you end up leaking DB internals into
  HTTP responses (e.g. SQLAlchemy relationship objects that can't be serialised
  to JSON), or you add HTTP validation logic to a model that has no business
  knowing about HTTP.

How FastAPI uses these:
  - Request body:  FastAPI parses the incoming JSON, validates it against the
                   schema, and injects a typed object into the route function.
                   If validation fails (wrong type, missing field), FastAPI
                   automatically returns a 422 Unprocessable Entity response
                   with details — no manual validation code needed.

  - Response body: FastAPI serialises the returned object to JSON using the
                   schema's field definitions. response_model= on the route
                   decorator strips any extra fields not defined in the schema
                   (prevents accidentally leaking internal fields).

  - /docs:         FastAPI reads the schema's field names, types, and docstrings
                   to auto-generate the OpenAPI documentation at /docs.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    """
    Body of POST /chat.

    Fields:
      message:         The user's text message. Required, cannot be empty.
      conversation_id: If provided, the reply is added to this existing
                       conversation and history is loaded. If None (omitted),
                       a new conversation is created automatically.
                       Clients store this value from the first response and
                       send it back on every subsequent turn.
    """

    message: str = Field(
        ...,  # ... means required — FastAPI returns 422 if omitted
        min_length=1,
        description="The user's message to send to the AI.",
        examples=["What is the capital of France?"],
    )

    conversation_id: str | None = Field(
        default=None,
        description=(
            "ID of an existing conversation to continue. "
            "Omit to start a new conversation."
        ),
        examples=["b8f3a21c-4d2e-4f1a-9c3b-7a1e5d9f2b0c"],
    )


class ChatResponse(BaseModel):
    """
    Body of the POST /chat response.

    Fields:
      conversation_id: Always returned. On a new conversation this is the
                       newly generated ID — the client must store it to
                       continue the conversation in the next request.
      reply:           The assistant's text response.
      tokens_used:     Total tokens consumed by this exchange (input + output).
                       Useful for cost estimation and Langfuse tracing.
      model:           The exact model name that generated the reply.
                       Useful when OLLAMA_MODEL changes between deploys —
                       clients can log which model produced a given response.
    """

    conversation_id: str = Field(
        description="ID of the conversation. Store this and send it back to continue."
    )
    reply: str = Field(description="The assistant's response text.")
    tokens_used: int = Field(
        description="Total tokens used (prompt + completion) for this exchange."
    )
    model: str = Field(description="The model that generated this response.")
    cache_hit: bool = Field(
        default=False,
        description=(
            "True if the response was served from the Redis cache instead of "
            "calling the AI model. Useful for debugging perf and verifying "
            "the cache is actually working."
        ),
    )


# ---------------------------------------------------------------------------
# Document Q&A (Step 13 — dspy.RLM)
# ---------------------------------------------------------------------------


class DocumentResponse(BaseModel):
    """
    Body of GET /documents (list) and POST /documents (after upload).

    Mirrors the Document ORM model but excludes content_text — full document
    text can be megabytes and is never returned by the list endpoint. To
    retrieve full text, query the chat endpoint with the document_id.
    """

    # from_attributes lets FastAPI build this directly from a SQLAlchemy ORM
    # object — no manual field mapping in the router.
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="UUID of the document.")
    filename: str = Field(description="Original upload filename.")
    content_type: str = Field(description="MIME type of the uploaded file.")
    size_bytes: int = Field(description="Original upload size in bytes.")
    created_at: datetime = Field(description="When the document was uploaded.")


class DocsChatRequest(BaseModel):
    """
    Body of POST /chat/docs.

    Fields:
      question:     The user's question about the uploaded document(s).
      document_ids: Restrict the RLM to these documents only. If omitted or
                    empty, all stored documents are loaded into the sandbox —
                    fine when storage is small (default cap is 10 MB total)
                    but can be slow/expensive for large corpora.
    """

    question: str = Field(
        ...,
        min_length=1,
        description="The question to ask of the uploaded documents.",
        examples=["What does the contract say about termination?"],
    )

    document_ids: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of document UUIDs to scope the search to. "
            "If omitted, all documents are visible to the RLM."
        ),
    )


class DocsChatResponse(BaseModel):
    """
    Body of POST /chat/docs.

    Fields:
      answer:           The RLM's final answer.
      documents_used:   IDs of documents the RLM was given access to. NOT a
                        list of which it actually read — RLM may or may not
                        touch every doc in the sandbox.
      iterations:       How many internal LM steps the RLM took. Useful for
                        debugging "why is this slow / expensive".
      total_tokens:     Sum of tokens used across all internal LM calls.
    """

    answer: str = Field(description="The RLM's answer to the question.")
    documents_used: list[str] = Field(
        description="Document UUIDs loaded into the RLM sandbox for this query."
    )
    iterations: int = Field(description="Number of internal LM steps the RLM took.")
    total_tokens: int = Field(
        description="Total tokens consumed across all internal LM calls."
    )


class AIServiceError(Exception):
    """
    Raised by app/services/ai.py when the Ollama call fails.

    Why a custom exception instead of letting the raw Ollama error propagate?
      1. The HTTP layer (routers/chat.py) catches this and returns a clean
         503 response. If raw SDK exceptions leaked up, the HTTP layer would
         need to know about Ollama internals — tight coupling.
      2. If we ever switch from Ollama to another provider, only ai.py changes.
         The router catches AIServiceError regardless of what's underneath.
      3. Raw SDK errors often contain internal details (URLs, credentials)
         that should never appear in HTTP responses.
    """

    def __init__(self, message: str, original: Exception | None = None) -> None:
        super().__init__(message)
        self.original = original  # preserved for logging, never sent to client
