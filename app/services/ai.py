"""
app/services/ai.py — Ollama AI service wrapper
===============================================
This is the ONLY file in the codebase that imports or knows about Ollama.
Every other module that needs AI inference calls this service's functions.

Why isolate the provider here?
  If we ever switch from Ollama to another provider (OpenAI, Anthropic, etc.),
  we change THIS file only. The router, tests, and history service are unaffected
  because they depend on this module's interface, not on Ollama directly.

How Ollama works
  Ollama is a local model runner. It downloads open-source models (Llama, Mistral,
  Gemma, etc.) and serves them via a REST API at http://localhost:11434.

  The Python client wraps that REST API. We use AsyncClient (not the sync Client)
  because our FastAPI app runs on an async event loop — a blocking (sync) HTTP call
  to Ollama inside an async route would freeze the event loop and prevent all other
  requests from being handled until the model responds.

Message format
  Ollama's chat API uses the same message format as OpenAI:
    [
      {"role": "user",      "content": "hello"},
      {"role": "assistant", "content": "hi there!"},
      {"role": "user",      "content": "how are you?"},
    ]
  We convert our ORM Message objects to this dict format before sending.

Error handling
  All Ollama exceptions are caught and re-raised as AIServiceError (defined in
  schemas.py). This prevents raw provider exceptions from leaking into the HTTP
  layer. The router catches AIServiceError and returns a clean 503 response.

Token counting
  Ollama returns token counts in the response:
    response.prompt_eval_count  — tokens in the input (prompt + history)
    response.eval_count         — tokens in the output (the reply)
  We store eval_count (output tokens) in the messages table and return the
  total (input + output) in the HTTP response for cost visibility.
"""

import time
from dataclasses import dataclass

import ollama
import structlog

from app.config import settings
from app.models.database import Message
from app.models.schemas import AIServiceError

logger = structlog.get_logger(__name__)


@dataclass
class AIResponse:
    """
    What ai.py returns to its callers.

    A plain dataclass (not Pydantic) because this is an internal data
    transfer object — it never travels over HTTP directly. The router
    converts it into a ChatResponse Pydantic model for the HTTP layer.

    Fields:
      reply:        The assistant's text response.
      tokens_used:  Output tokens only (what the model generated).
                    This is what gets stored in the messages table.
      total_tokens: Input tokens + output tokens. Returned to the client
                    so they can track their total usage per request.
      model:        The exact model name Ollama used. Useful when
                    OLLAMA_MODEL changes between deploys.
    """
    reply: str
    tokens_used: int       # output tokens — stored per message in DB
    total_tokens: int      # input + output — returned in HTTP response
    model: str


def _messages_to_ollama_format(
    history: list[Message],
    new_user_message: str,
) -> list[dict[str, str]]:
    """
    Convert ORM Message objects + the new user message into the dict format
    that Ollama's chat API expects.

    Ollama (and OpenAI-compatible APIs) expect:
      [{"role": "user" | "assistant", "content": "<text>"}]

    The new user message is appended at the end so the full conversation
    context — history first, then the new message — is sent to the model.
    The model uses the full sequence to generate a contextually relevant reply.

    Why not send just the new message?
      Without history, every message is treated as the start of a new
      conversation. The model has no context: "What did you mean?" would
      get a confused response because the model doesn't know what "you"
      refers to.
    """
    messages = [
        {"role": msg.role.value, "content": msg.content}
        for msg in history
    ]
    messages.append({"role": "user", "content": new_user_message})
    return messages


async def get_ai_reply(
    history: list[Message],
    new_user_message: str,
    conversation_id: str,
) -> AIResponse:
    """
    Send the conversation history + new message to Ollama and return the reply.

    Args:
        history:          Previous messages in this conversation (oldest first).
                          May be empty for a new conversation.
        new_user_message: The user's latest message text.
        conversation_id:  Used only for structured logging — lets us correlate
                          AI call logs with request logs via the same ID.

    Returns:
        AIResponse with the reply text, token counts, and model name.

    Raises:
        AIServiceError: if Ollama is unreachable or returns an error.
                        The router catches this and returns HTTP 503.
    """
    messages = _messages_to_ollama_format(history, new_user_message)

    # Create a new AsyncClient per call rather than a module-level singleton.
    # Reason: Ollama's AsyncClient does not maintain persistent connections —
    # each call opens and closes an HTTP connection anyway. A module-level
    # client would not provide connection pooling benefits here, and would
    # complicate testing (harder to mock).
    client = ollama.AsyncClient(host=settings.ollama_base_url)

    start_time = time.monotonic()

    try:
        response = await client.chat(
            model=settings.ollama_model,
            messages=messages,  # type: ignore[arg-type]
        )
    except ollama.ResponseError as exc:
        # ResponseError: Ollama is running but returned an error (e.g. model
        # not found — the model wasn't pulled with `ollama pull <model>`).
        await logger.aerror(
            "ollama response error",
            conversation_id=conversation_id,
            model=settings.ollama_model,
            error=str(exc),
        )
        raise AIServiceError(
            f"AI model error: {exc.error}",
            original=exc,
        ) from exc
    except Exception as exc:
        # Catches connection errors (Ollama not running, wrong URL, network
        # timeouts). We log the original exception for debugging but raise
        # a clean AIServiceError so the HTTP layer stays decoupled from Ollama.
        await logger.aerror(
            "ollama unreachable",
            conversation_id=conversation_id,
            model=settings.ollama_model,
            error=str(exc),
        )
        raise AIServiceError(
            "AI service is unavailable. Is Ollama running?",
            original=exc,
        ) from exc

    duration_ms = round((time.monotonic() - start_time) * 1000, 2)

    # Extract token counts from Ollama's response.
    # These fields are set by Ollama after inference; they may be 0 if the
    # model doesn't report them (some smaller models omit token counts).
    output_tokens = response.eval_count or 0
    input_tokens = response.prompt_eval_count or 0
    total_tokens = input_tokens + output_tokens

    reply_text = response.message.content or ""

    await logger.ainfo(
        "ai reply received",
        conversation_id=conversation_id,
        model=settings.ollama_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        duration_ms=duration_ms,
        # Log content length not content — replies may contain sensitive info
        reply_length=len(reply_text),
    )

    return AIResponse(
        reply=reply_text,
        tokens_used=output_tokens,
        total_tokens=total_tokens,
        model=settings.ollama_model,
    )
