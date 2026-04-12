"""
tests/unit/test_ai_service.py — unit tests for the Ollama AI service
======================================================================
The cardinal rule for AI service tests: NEVER call the real AI API.

Why:
  1. Speed — a real Ollama call takes 2-30s. A mock takes <1ms.
  2. Cost — even "free" local models consume GPU time and memory.
  3. Flakiness — tests should pass whether or not Ollama is running.
  4. Isolation — we test OUR code's behaviour, not Ollama's correctness.

Mocking strategy
----------------
We patch `ollama.AsyncClient` at the point it is used (inside app.services.ai),
not where it is defined (in the ollama package). This is the correct approach:

  patch("app.services.ai.ollama.AsyncClient", ...)

This replaces the AsyncClient class in the ai module's namespace for the
duration of each test. When ai.py does `client = ollama.AsyncClient(...)`,
it gets our mock instead.

What we test here:
  - Message history is converted to the correct Ollama format (role + content)
  - The new user message is appended after the history
  - Token counts are extracted correctly from the response
  - ResponseError (model not found) raises AIServiceError
  - Connection errors (Ollama not running) raise AIServiceError
  - The function returns an AIResponse with the expected fields
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.database import Message, MessageRole
from app.models.schemas import AIServiceError
from app.services.ai import AIResponse, get_ai_reply, _messages_to_ollama_format


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_message(role: MessageRole, content: str) -> Message:
    """Build a minimal ORM Message object for use in tests."""
    msg = MagicMock(spec=Message)
    msg.role = role
    msg.content = content
    return msg


def make_ollama_response(
    content: str = "Hello!",
    prompt_eval_count: int = 10,
    eval_count: int = 5,
    model: str = "llama3.2:cloud",
) -> MagicMock:
    """
    Build a mock that looks like an ollama chat() response.

    Ollama's response object has:
      response.message.content        — the reply text
      response.prompt_eval_count      — input token count
      response.eval_count             — output token count
    """
    response = MagicMock()
    response.message.content = content
    response.prompt_eval_count = prompt_eval_count
    response.eval_count = eval_count
    response.model = model
    return response


# ---------------------------------------------------------------------------
# Tests for _messages_to_ollama_format (pure function, no mocking needed)
# ---------------------------------------------------------------------------

class TestMessagesToOllamaFormat:

    def test_empty_history_produces_single_user_message(self) -> None:
        """
        GIVEN: No prior conversation history
        WHEN:  _messages_to_ollama_format is called
        THEN:  Returns exactly one message — the new user message

        This is the first-turn case. Ollama receives no prior context,
        just the user's opening message.
        """
        result = _messages_to_ollama_format([], "hello")

        assert result == [{"role": "user", "content": "hello"}]

    def test_history_is_prepended_before_new_message(self) -> None:
        """
        GIVEN: Two prior messages (user + assistant)
        WHEN:  _messages_to_ollama_format is called with a new message
        THEN:  History comes first, new message is last

        Order is critical for the AI: it must see the conversation in
        chronological order. If the new message appeared before the history,
        the model would see the most recent message as the "first" one and
        generate incoherent context-aware replies.
        """
        history = [
            make_message(MessageRole.USER, "What is 2+2?"),
            make_message(MessageRole.ASSISTANT, "4"),
        ]

        result = _messages_to_ollama_format(history, "Are you sure?")

        assert result == [
            {"role": "user",      "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
            {"role": "user",      "content": "Are you sure?"},
        ]

    def test_role_values_are_strings_not_enum(self) -> None:
        """
        GIVEN: A history message with a MessageRole enum value
        WHEN:  Converted to Ollama format
        THEN:  role is a plain string ("user"), not the enum ("MessageRole.USER")

        Ollama's API expects plain strings. If we passed the enum object,
        it would serialise to "user" anyway (since MessageRole(str, enum.Enum)),
        but this test makes the expectation explicit and guards against
        a future refactor that might break the .value call.
        """
        history = [make_message(MessageRole.ASSISTANT, "Hi")]
        result = _messages_to_ollama_format(history, "hello")

        assert result[0]["role"] == "assistant"
        assert isinstance(result[0]["role"], str)


# ---------------------------------------------------------------------------
# Tests for get_ai_reply (mocks the Ollama client)
# ---------------------------------------------------------------------------

class TestGetAiReply:

    @pytest.mark.asyncio
    async def test_returns_ai_response_on_success(self) -> None:
        """
        GIVEN: Ollama responds successfully
        WHEN:  get_ai_reply is called
        THEN:  Returns an AIResponse with the reply text and token counts
        """
        mock_response = make_ollama_response(
            content="Paris is the capital of France.",
            prompt_eval_count=8,
            eval_count=7,
        )

        with patch("app.services.ai.ollama.AsyncClient") as MockClient:
            MockClient.return_value.chat = AsyncMock(return_value=mock_response)

            result = await get_ai_reply(
                history=[],
                new_user_message="What is the capital of France?",
                conversation_id="test-conv-id",
            )

        assert isinstance(result, AIResponse)
        assert result.reply == "Paris is the capital of France."
        assert result.tokens_used == 7        # output tokens only (eval_count)
        assert result.total_tokens == 15      # input + output (8 + 7)
        assert result.model == "llama3.2:cloud"

    @pytest.mark.asyncio
    async def test_raises_ai_service_error_on_response_error(self) -> None:
        """
        GIVEN: Ollama returns a ResponseError (e.g. model not pulled yet)
        WHEN:  get_ai_reply is called
        THEN:  Raises AIServiceError — not the raw ollama exception

        This verifies the error translation layer. The HTTP router catches
        AIServiceError; if the raw ollama.ResponseError leaked through, the
        router would need to know about Ollama internals — breaking isolation.
        """
        import ollama as ollama_pkg

        with patch("app.services.ai.ollama.AsyncClient") as MockClient:
            MockClient.return_value.chat = AsyncMock(
                side_effect=ollama_pkg.ResponseError("model 'llama3.2' not found")
            )

            with pytest.raises(AIServiceError) as exc_info:
                await get_ai_reply(
                    history=[],
                    new_user_message="hello",
                    conversation_id="test-conv-id",
                )

        assert "AI model error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_ai_service_error_on_connection_error(self) -> None:
        """
        GIVEN: Ollama is not running (connection refused)
        WHEN:  get_ai_reply is called
        THEN:  Raises AIServiceError with a user-friendly message

        Without this translation, a raw ConnectionRefusedError would propagate
        to the HTTP layer and cause an unhandled 500 instead of a clean 503.
        """
        with patch("app.services.ai.ollama.AsyncClient") as MockClient:
            MockClient.return_value.chat = AsyncMock(
                side_effect=ConnectionRefusedError("Connection refused")
            )

            with pytest.raises(AIServiceError) as exc_info:
                await get_ai_reply(
                    history=[],
                    new_user_message="hello",
                    conversation_id="test-conv-id",
                )

        assert "unavailable" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_passes_full_history_to_ollama(self) -> None:
        """
        GIVEN: Two prior messages in history
        WHEN:  get_ai_reply is called
        THEN:  Ollama client.chat() receives all 3 messages (history + new)

        This guards against a bug where history is accidentally dropped —
        the model would have no context and produce irrelevant replies.
        """
        history = [
            make_message(MessageRole.USER, "Hello"),
            make_message(MessageRole.ASSISTANT, "Hi there!"),
        ]
        mock_response = make_ollama_response()

        with patch("app.services.ai.ollama.AsyncClient") as MockClient:
            mock_chat = AsyncMock(return_value=mock_response)
            MockClient.return_value.chat = mock_chat

            await get_ai_reply(
                history=history,
                new_user_message="How are you?",
                conversation_id="test-conv-id",
            )

        # Extract the messages argument passed to client.chat()
        call_kwargs = mock_chat.call_args.kwargs
        messages_sent = call_kwargs["messages"]

        assert len(messages_sent) == 3
        assert messages_sent[0] == {"role": "user",      "content": "Hello"}
        assert messages_sent[1] == {"role": "assistant", "content": "Hi there!"}
        assert messages_sent[2] == {"role": "user",      "content": "How are you?"}
