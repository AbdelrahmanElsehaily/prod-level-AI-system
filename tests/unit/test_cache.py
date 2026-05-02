"""
tests/unit/test_cache.py — unit tests for the AI response cache
================================================================
Tests cover:
  - Key derivation: deterministic, sensitive to model + messages
  - get_cached / set_cached round-trip with a fakeredis backend
  - JSON corruption returns None (treated as miss)
  - Redis errors are swallowed (fail-open) and never raise
  - get_ai_reply hits the cache on second identical call
  - get_ai_reply with no Redis client behaves as before (no-cache mode)
  - Empty replies are NOT cached (avoids freezing transient model glitches)

We use fakeredis instead of mocking Redis methods one-by-one — fakeredis
implements the real Redis protocol in memory, so the cache module's calls
exercise the real redis-py code path. This catches bugs that AsyncMock would
silently paper over.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis

from app.models.database import MessageRole
from app.services import cache
from app.services.ai import get_ai_reply

# Reuse helpers from the ai_service tests.
from tests.unit.test_ai_service import make_message, make_ollama_response

# Note: pytest-asyncio is configured in pyproject.toml as `asyncio_mode = "auto"`,
# so async test functions are detected automatically — no per-module marker needed.

# ---------------------------------------------------------------------------
# make_cache_key
# ---------------------------------------------------------------------------


class TestMakeCacheKey:
    def test_same_input_same_key(self) -> None:
        """Same model + same messages must always produce the same key."""
        messages = [{"role": "user", "content": "hi"}]
        assert cache.make_cache_key("m1", messages) == cache.make_cache_key(
            "m1", messages
        )

    def test_different_model_different_key(self) -> None:
        """Changing the model must change the key — switching models must invalidate."""
        messages = [{"role": "user", "content": "hi"}]
        assert cache.make_cache_key("m1", messages) != cache.make_cache_key(
            "m2", messages
        )

    def test_different_messages_different_key(self) -> None:
        assert cache.make_cache_key(
            "m1", [{"role": "user", "content": "hi"}]
        ) != cache.make_cache_key("m1", [{"role": "user", "content": "bye"}])

    def test_history_changes_the_key(self) -> None:
        """Same final message but different history → different key (option A)."""
        without_history = [{"role": "user", "content": "tell me more"}]
        with_history = [
            {"role": "user", "content": "what is python?"},
            {"role": "assistant", "content": "a programming language"},
            {"role": "user", "content": "tell me more"},
        ]
        assert cache.make_cache_key("m1", without_history) != cache.make_cache_key(
            "m1", with_history
        )

    def test_key_is_namespaced(self) -> None:
        """All keys share the prefix so we can flush only chat cache, not other Redis data."""
        key = cache.make_cache_key("m1", [{"role": "user", "content": "hi"}])
        assert key.startswith("chat:cache:")


# ---------------------------------------------------------------------------
# get_cached / set_cached
# ---------------------------------------------------------------------------


class TestCacheRoundTrip:
    async def test_set_then_get_returns_value(self) -> None:
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await cache.set_cached(client, "k", {"reply": "yo", "tokens_used": 1})
        assert await cache.get_cached(client, "k") == {"reply": "yo", "tokens_used": 1}

    async def test_get_missing_key_returns_none(self) -> None:
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        assert await cache.get_cached(client, "does-not-exist") is None

    async def test_set_applies_ttl(self) -> None:
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await cache.set_cached(client, "k", {"reply": "x"}, ttl_seconds=42)
        # Redis returns remaining TTL in seconds. Should be ≤ what we set.
        ttl = await client.ttl("k")
        assert 0 < ttl <= 42

    async def test_corrupted_json_returns_none(self) -> None:
        """If something writes garbage to a cache key, treat it as a miss."""
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await client.set("k", "not-json-{{{")
        assert await cache.get_cached(client, "k") is None

    async def test_get_swallows_redis_errors(self) -> None:
        """Redis exceptions must NOT propagate — fail-open."""
        broken = AsyncMock()
        broken.get.side_effect = ConnectionError("Redis is down")
        # Should return None, not raise.
        assert await cache.get_cached(broken, "k") is None

    async def test_set_swallows_redis_errors(self) -> None:
        broken = AsyncMock()
        broken.set.side_effect = ConnectionError("Redis is down")
        # Should return cleanly, not raise.
        await cache.set_cached(broken, "k", {"reply": "x"})


# ---------------------------------------------------------------------------
# get_ai_reply integration with cache
# ---------------------------------------------------------------------------


class TestAIReplyCaching:
    async def test_cache_hit_skips_ollama_call(self) -> None:
        """
        Pre-populate the cache with a known value, then call get_ai_reply.
        It must return the cached value AND must not have called Ollama.
        """
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)

        # Compute the same key the service will compute.
        history = [make_message(MessageRole.USER, "hi")]
        new_msg = "hello again"
        from app.config import settings

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "user", "content": new_msg},
        ]
        key = cache.make_cache_key(settings.ollama_model, messages)

        await client.set(
            key,
            json.dumps(
                {
                    "reply": "cached reply",
                    "tokens_used": 7,
                    "total_tokens": 12,
                    "model": settings.ollama_model,
                }
            ),
        )

        # Patch the Ollama client so we'd notice if it gets called.
        with patch("app.services.ai.ollama.AsyncClient") as mock_async_client:
            mock_instance = MagicMock()
            mock_instance.chat = AsyncMock()  # would fail the assertion if called
            mock_async_client.return_value = mock_instance

            result = await get_ai_reply(
                history=history,
                new_user_message=new_msg,
                conversation_id="conv-1",
                redis_client=client,
            )

            # Ollama was NOT called.
            mock_instance.chat.assert_not_called()

        assert result.cache_hit is True
        assert result.reply == "cached reply"
        assert result.tokens_used == 7
        assert result.total_tokens == 12

    async def test_cache_miss_calls_ollama_and_stores(self) -> None:
        """First call: cache empty → Ollama called → result stored in cache."""
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)

        # Patch langfuse to None so the real Langfuse SDK doesn't try to
        # validate our test conversation_id as a 32-char hex trace ID.
        with (
            patch("app.services.ai.ollama.AsyncClient") as mock_async_client,
            patch("app.services.ai.langfuse", None),
        ):
            mock_instance = MagicMock()
            mock_instance.chat = AsyncMock(
                return_value=make_ollama_response(content="fresh reply")
            )
            mock_async_client.return_value = mock_instance

            result = await get_ai_reply(
                history=[],
                new_user_message="hello",
                conversation_id="test-conv-id",
                redis_client=client,
            )

            mock_instance.chat.assert_awaited_once()

        assert result.cache_hit is False
        assert result.reply == "fresh reply"

        # The reply is now in Redis under the right key.
        from app.config import settings

        key = cache.make_cache_key(
            settings.ollama_model, [{"role": "user", "content": "hello"}]
        )
        stored = await client.get(key)
        assert stored is not None
        assert json.loads(stored)["reply"] == "fresh reply"

    async def test_no_redis_client_skips_cache_entirely(self) -> None:
        """Calling without a redis_client must work (legacy/no-cache mode)."""
        with (
            patch("app.services.ai.ollama.AsyncClient") as mock_async_client,
            patch("app.services.ai.langfuse", None),
        ):
            mock_instance = MagicMock()
            mock_instance.chat = AsyncMock(
                return_value=make_ollama_response(content="ok")
            )
            mock_async_client.return_value = mock_instance

            result = await get_ai_reply(
                history=[],
                new_user_message="hi",
                conversation_id="test-conv-id",
                # redis_client omitted on purpose
            )

        assert result.cache_hit is False
        assert result.reply == "ok"

    async def test_empty_reply_is_not_cached(self) -> None:
        """Don't freeze a transient empty response into the cache for an hour."""
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)

        with (
            patch("app.services.ai.ollama.AsyncClient") as mock_async_client,
            patch("app.services.ai.langfuse", None),
        ):
            mock_instance = MagicMock()
            mock_instance.chat = AsyncMock(
                return_value=make_ollama_response(content="")
            )
            mock_async_client.return_value = mock_instance

            await get_ai_reply(
                history=[],
                new_user_message="hi",
                conversation_id="test-conv-id",
                redis_client=client,
            )

        # Nothing should be cached.
        from app.config import settings

        key = cache.make_cache_key(
            settings.ollama_model, [{"role": "user", "content": "hi"}]
        )
        assert await client.get(key) is None
