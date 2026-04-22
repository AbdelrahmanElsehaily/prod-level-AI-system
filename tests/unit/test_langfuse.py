"""
tests/unit/test_langfuse.py — unit tests for Langfuse AI tracing
=================================================================
Two concerns tested here:

1. langfuse_client module behaviour
   - No credentials → langfuse singleton is None (no-op)
   - Credentials set → Langfuse() is called with the right keys
   - flush() drains the queue when Langfuse is active
   - flush() is a no-op when Langfuse is None

2. get_ai_reply() tracing integration
   - A generation span is started before the Ollama call
   - The span is closed with output + token counts on success
   - The span is closed with ERROR level on Ollama failure
   - No span is created when Langfuse is disabled

Why mock Langfuse?
  Same reason we mock sentry_sdk.init() and ollama.AsyncClient — we never
  want unit tests to open a real network connection to an external service.
  Mocking lets us assert the exact arguments passed to Langfuse's SDK
  without any I/O.

Mocking strategy for langfuse_client
  The langfuse singleton is initialised at module import time. We cannot
  simply patch the constructor call that already happened. Instead we patch
  the `langfuse` name inside the modules that USE it:

    patch("app.services.ai.langfuse", mock_langfuse_instance)
    patch("app.langfuse_client.langfuse", mock_langfuse_instance)

  This replaces the reference in the target module's namespace for the
  duration of the test — the same pattern as patching at the point of use.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.database import MessageRole
from app.models.schemas import AIServiceError
from app.services.ai import get_ai_reply

# ---------------------------------------------------------------------------
# Helpers (shared with test_ai_service.py but copied here for independence)
# ---------------------------------------------------------------------------


def make_message(role: MessageRole, content: str) -> MagicMock:
    """Build a minimal ORM Message mock for use in tests."""
    msg = MagicMock()
    msg.role = role
    msg.content = content
    return msg


def make_ollama_response(
    content: str = "Hello!",
    prompt_eval_count: int = 10,
    eval_count: int = 5,
) -> MagicMock:
    """Build a mock Ollama chat() response."""
    response = MagicMock()
    response.message.content = content
    response.prompt_eval_count = prompt_eval_count
    response.eval_count = eval_count
    return response


def make_langfuse_mock() -> MagicMock:
    """
    Build a mock Langfuse client instance.

    generation() returns a mock generation object with an end() method.
    We capture calls to generation() and end() to assert they were called
    with the expected arguments.
    """
    mock_lf = MagicMock()
    mock_generation = MagicMock()
    mock_lf.generation.return_value = mock_generation
    return mock_lf


# ---------------------------------------------------------------------------
# langfuse_client module tests
# ---------------------------------------------------------------------------


class TestLangfuseClientInit:
    """Verify init_langfuse() initialisation logic."""

    def test_no_credentials_returns_none(self) -> None:
        """
        When LANGFUSE_SECRET_KEY is unset, init_langfuse() must return None.
        We call it directly (no module reload needed) with patched settings.

        Why test init_langfuse() instead of the module-level singleton?
          The singleton is evaluated at import time — before any test can patch
          settings. init_langfuse() is a plain callable that reads settings
          at call time, so we can patch settings and call it repeatedly in tests.
          Same pattern as testing init_sentry() in test_sentry.py.
        """
        from app.langfuse_client import init_langfuse

        with patch("app.langfuse_client.settings") as mock_settings:
            mock_settings.langfuse_secret_key = None
            mock_settings.langfuse_public_key = None

            result = init_langfuse()

        assert result is None

    def test_with_credentials_calls_langfuse_constructor(self) -> None:
        """
        When both keys are set, init_langfuse() calls Langfuse() exactly once
        and returns the resulting client instance.

        We patch "langfuse.Langfuse" (the import source inside the function)
        because init_langfuse() does `from langfuse import Langfuse` on every
        call — patching the source ensures the function gets our mock.
        """
        from app.langfuse_client import init_langfuse

        with patch("app.langfuse_client.settings") as mock_settings:
            mock_settings.langfuse_secret_key = "sk-test-secret"
            mock_settings.langfuse_public_key = "pk-test-public"

            with patch("langfuse.Langfuse") as mock_cls:
                mock_cls.return_value = MagicMock()
                result = init_langfuse()

            mock_cls.assert_called_once_with(
                secret_key="sk-test-secret",
                public_key="pk-test-public",
            )
        assert result is mock_cls.return_value

    def test_flush_calls_langfuse_flush_when_active(self) -> None:
        """
        flush() must call langfuse.flush() when the client is not None.
        """
        from app.langfuse_client import flush

        mock_lf = MagicMock()
        with patch("app.langfuse_client.langfuse", mock_lf):
            flush()

        mock_lf.flush.assert_called_once()

    def test_flush_is_noop_when_langfuse_is_none(self) -> None:
        """
        flush() must not raise when langfuse is None (unconfigured).
        """
        from app.langfuse_client import flush

        with patch("app.langfuse_client.langfuse", None):
            flush()  # must not raise


# ---------------------------------------------------------------------------
# get_ai_reply() Langfuse integration tests
# ---------------------------------------------------------------------------


class TestGetAiReplyLangfuseIntegration:
    """Verify Langfuse tracing calls inside get_ai_reply()."""

    @pytest.mark.asyncio
    async def test_generation_started_before_ollama_call(self) -> None:
        """
        A Langfuse generation span must be created before the Ollama call
        so we always capture timing from the very start of the request.
        """
        mock_lf = make_langfuse_mock()
        mock_response = make_ollama_response()

        with (
            patch("app.services.ai.langfuse", mock_lf),
            patch("app.services.ai.ollama.AsyncClient") as mock_client,
        ):
            mock_client.return_value.chat = AsyncMock(return_value=mock_response)
            await get_ai_reply(
                history=[],
                new_user_message="hello",
                conversation_id="conv-abc",
            )

        # generation() must have been called once
        mock_lf.generation.assert_called_once()
        call_kwargs = mock_lf.generation.call_args.kwargs
        assert call_kwargs["trace_id"] == "conv-abc"
        assert call_kwargs["model"] is not None

    @pytest.mark.asyncio
    async def test_generation_closed_with_output_on_success(self) -> None:
        """
        After a successful Ollama call, generation.end() must be called
        with the reply text and token usage.
        """
        mock_lf = make_langfuse_mock()
        mock_generation = mock_lf.generation.return_value
        mock_response = make_ollama_response(
            content="The answer is 42.",
            prompt_eval_count=8,
            eval_count=6,
        )

        with (
            patch("app.services.ai.langfuse", mock_lf),
            patch("app.services.ai.ollama.AsyncClient") as mock_client,
        ):
            mock_client.return_value.chat = AsyncMock(return_value=mock_response)
            await get_ai_reply(
                history=[],
                new_user_message="What is the answer?",
                conversation_id="conv-abc",
            )

        mock_generation.end.assert_called_once()
        end_kwargs = mock_generation.end.call_args.kwargs

        # Output must be the reply text
        assert end_kwargs["output"] == "The answer is 42."

        # Usage must contain input/output/total token counts
        usage = end_kwargs["usage"]
        assert usage["input"] == 8
        assert usage["output"] == 6
        assert usage["total"] == 14

    @pytest.mark.asyncio
    async def test_generation_closed_with_error_on_ollama_failure(self) -> None:
        """
        When Ollama raises, generation.end() must still be called with
        level="ERROR" so the span shows as failed in the Langfuse dashboard.
        """
        import ollama as ollama_pkg

        mock_lf = make_langfuse_mock()
        mock_generation = mock_lf.generation.return_value

        with (
            patch("app.services.ai.langfuse", mock_lf),
            patch("app.services.ai.ollama.AsyncClient") as mock_client,
        ):
            mock_client.return_value.chat = AsyncMock(
                side_effect=ollama_pkg.ResponseError("model not found")
            )

            with pytest.raises(AIServiceError):
                await get_ai_reply(
                    history=[],
                    new_user_message="hello",
                    conversation_id="conv-abc",
                )

        mock_generation.end.assert_called_once()
        end_kwargs = mock_generation.end.call_args.kwargs
        assert end_kwargs["level"] == "ERROR"

    @pytest.mark.asyncio
    async def test_no_generation_when_langfuse_disabled(self) -> None:
        """
        When Langfuse is not configured (langfuse is None), no generation
        object is created. The Ollama call must still succeed normally.
        """
        mock_response = make_ollama_response(content="I'm fine, thanks!")

        with (
            patch("app.services.ai.langfuse", None),
            patch("app.services.ai.ollama.AsyncClient") as mock_client,
        ):
            mock_client.return_value.chat = AsyncMock(return_value=mock_response)
            result = await get_ai_reply(
                history=[],
                new_user_message="How are you?",
                conversation_id="conv-xyz",
            )

        # The function must still return a valid AIResponse
        assert result.reply == "I'm fine, thanks!"

    @pytest.mark.asyncio
    async def test_trace_id_is_conversation_id(self) -> None:
        """
        The Langfuse trace_id must equal the conversation_id so all AI calls
        within one conversation are grouped into one trace in the dashboard.
        """
        mock_lf = make_langfuse_mock()
        mock_response = make_ollama_response()

        with (
            patch("app.services.ai.langfuse", mock_lf),
            patch("app.services.ai.ollama.AsyncClient") as mock_client,
        ):
            mock_client.return_value.chat = AsyncMock(return_value=mock_response)
            await get_ai_reply(
                history=[],
                new_user_message="hi",
                conversation_id="my-unique-conv-id",
            )

        call_kwargs = mock_lf.generation.call_args.kwargs
        assert call_kwargs["trace_id"] == "my-unique-conv-id"

    @pytest.mark.asyncio
    async def test_input_messages_sent_to_langfuse(self) -> None:
        """
        The full prompt (history + new message) sent to Ollama must also be
        recorded in the Langfuse generation so traces show the complete context
        — not just the user's latest message.
        """
        mock_lf = make_langfuse_mock()
        mock_response = make_ollama_response()

        history = [
            make_message(MessageRole.USER, "Hello"),
            make_message(MessageRole.ASSISTANT, "Hi there!"),
        ]

        with (
            patch("app.services.ai.langfuse", mock_lf),
            patch("app.services.ai.ollama.AsyncClient") as mock_client,
        ):
            mock_client.return_value.chat = AsyncMock(return_value=mock_response)
            await get_ai_reply(
                history=history,
                new_user_message="How are you?",
                conversation_id="conv-abc",
            )

        call_kwargs = mock_lf.generation.call_args.kwargs
        # input must be the full 3-message list (history + new)
        assert len(call_kwargs["input"]) == 3
        assert call_kwargs["input"][2]["content"] == "How are you?"
