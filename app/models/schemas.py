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

from pydantic import BaseModel, Field


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
    reply: str = Field(
        description="The assistant's response text."
    )
    tokens_used: int = Field(
        description="Total tokens used (prompt + completion) for this exchange."
    )
    model: str = Field(
        description="The model that generated this response."
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
