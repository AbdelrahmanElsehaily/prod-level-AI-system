"""
tests/unit/test_langfuse.py — unit tests for Langfuse AI tracing
=================================================================
Two concerns tested here:

1. langfuse_client module behaviour
   - No credentials → init_langfuse() returns None (no-op)
   - Credentials set → Langfuse() is called with the right keys
   - flush() drains the queue when Langfuse is active
   - flush() is a no-op when Langfuse is None

2. get_ai_reply() tracing integration (Langfuse v4 API)
   - start_as_current_observation() is called after a successful Ollama call
   - update_current_generation() receives the reply text and token usage
   - No observation is created when Langfuse is disabled
   - trace_id matches conversation_id
   - Input messages are recorded on the observation

Langfuse v4 API used in ai.py:
  with langfuse.start_as_current_observation(as_type="generation", ...) as obs:
      langfuse.update_current_generation(output=..., usage_details=...)

Why mock Langfuse?
  Same reason we mock sentry_sdk.init() and ollama.AsyncClient — we never
  want unit tests to open a real network connection to an external service.
  Mocking lets us assert the exact arguments passed to the Langfuse SDK
  without any I/O.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.database import MessageRole
from app.models.schemas import AIServiceError
from app.services.ai import get_ai_reply

# ---------------------------------------------------------------------------
# Helpers
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
    Build a mock Langfuse client matching the v4 API:

      langfuse.start_as_current_observation(...) → sync context manager
      langfuse.update_current_generation(output=..., usage_details=...)

    start_as_current_observation returns a context manager, so we configure
    its __enter__ / __exit__ to behave correctly.
    """
    mock_lf = MagicMock()

    # start_as_current_observation is used as a sync context manager.
    # __enter__ returns the observation mock, __exit__ is a no-op.
    mock_obs = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_obs)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    mock_lf.start_as_current_observation.return_value = mock_ctx

    return mock_lf


# ---------------------------------------------------------------------------
# langfuse_client module tests
# ---------------------------------------------------------------------------


class TestLangfuseClientInit:
    """Verify init_langfuse() initialisation logic."""

    def test_no_credentials_returns_none(self) -> None:
        """
        When LANGFUSE_SECRET_KEY is unset, init_langfuse() must return None.
        """
        from app.langfuse_client import init_langfuse

        with patch("app.langfuse_client.settings") as mock_settings:
            mock_settings.langfuse_secret_key = None
            mock_settings.langfuse_public_key = None
            result = init_langfuse()

        assert result is None

    def test_with_credentials_calls_langfuse_constructor(self) -> None:
        """
        When both keys are set, init_langfuse() calls Langfuse() once and
        returns the resulting client instance.
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
        """flush() must call langfuse.flush() when the client is not None."""
        from app.langfuse_client import flush

        mock_lf = MagicMock()
        with patch("app.langfuse_client.langfuse", mock_lf):
            flush()

        mock_lf.flush.assert_called_once()

    def test_flush_is_noop_when_langfuse_is_none(self) -> None:
        """flush() must not raise when langfuse is None (unconfigured)."""
        from app.langfuse_client import flush

        with patch("app.langfuse_client.langfuse", None):
            flush()  # must not raise


# ---------------------------------------------------------------------------
# get_ai_reply() Langfuse v4 integration tests
# ---------------------------------------------------------------------------


class TestGetAiReplyLangfuseIntegration:
    """Verify Langfuse v4 tracing calls inside get_ai_reply()."""

    @pytest.mark.asyncio
    async def test_observation_started_after_successful_ollama_call(self) -> None:
        """
        start_as_current_observation() must be called once after a successful
        Ollama response with the correct type and model.
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

        mock_lf.start_as_current_observation.assert_called_once()
        call_kwargs = mock_lf.start_as_current_observation.call_args.kwargs
        assert call_kwargs["as_type"] == "generation"
        assert call_kwargs["trace_context"]["trace_id"] == "conv-abc".replace("-", "")
        assert call_kwargs["model"] is not None

    @pytest.mark.asyncio
    async def test_update_current_generation_called_with_output(self) -> None:
        """
        update_current_generation() must be called with the reply text and
        token usage after a successful Ollama call.
        """
        mock_lf = make_langfuse_mock()
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

        mock_lf.update_current_generation.assert_called_once()
        update_kwargs = mock_lf.update_current_generation.call_args.kwargs

        assert update_kwargs["output"] == "The answer is 42."
        usage = update_kwargs["usage_details"]
        assert usage["input"] == 8
        assert usage["output"] == 6
        assert usage["total"] == 14

    @pytest.mark.asyncio
    async def test_no_observation_when_langfuse_disabled(self) -> None:
        """
        When Langfuse is None, no observation is created and the Ollama
        call still succeeds normally.
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

        assert result.reply == "I'm fine, thanks!"

    @pytest.mark.asyncio
    async def test_no_observation_on_ollama_failure(self) -> None:
        """
        When Ollama raises an error, start_as_current_observation() must NOT
        be called — we only record successful calls in Langfuse to keep the
        error path simple.
        """
        import ollama as ollama_pkg

        mock_lf = make_langfuse_mock()

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

        mock_lf.start_as_current_observation.assert_not_called()

    @pytest.mark.asyncio
    async def test_trace_id_is_conversation_id(self) -> None:
        """
        trace_id in the observation must equal the conversation_id so all AI
        calls in one conversation are grouped into one trace in the dashboard.
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

        call_kwargs = mock_lf.start_as_current_observation.call_args.kwargs
        assert call_kwargs["trace_context"]["trace_id"] == "my-unique-conv-id".replace(
            "-", ""
        )

    @pytest.mark.asyncio
    async def test_input_messages_recorded_in_observation(self) -> None:
        """
        The full prompt (history + new message) must be recorded as the
        observation's input so traces show the complete context.
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

        call_kwargs = mock_lf.start_as_current_observation.call_args.kwargs
        # input must be the full 3-message list (2 history + 1 new)
        assert len(call_kwargs["input"]) == 3
        assert call_kwargs["input"][2]["content"] == "How are you?"
