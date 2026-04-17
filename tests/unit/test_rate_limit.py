"""
tests/unit/test_rate_limit.py — unit tests for the rate limit middleware
=========================================================================
These tests verify the rate limiting behaviour without a real Redis instance.
We use a fake Redis client (built with MagicMock) that tracks what keys were
written and returns controlled responses — no network, no Docker required.

Testing strategy
----------------
Rate limiting logic lives entirely in the middleware. The tests exercise:
  1. Normal requests — headers are present, 200 is returned
  2. Exceeding the limit — 21st request returns 429 with correct body/headers
  3. Health endpoint exclusion — /health is never blocked
  4. Redis failure — middleware fails open (lets request through)

Why not use a real Redis here?
  Unit tests must be fast (< 1 second total) and runnable without any
  external services. Real Redis tests belong in tests/integration/.
  Here we control Redis's responses exactly — we can simulate "15 existing
  requests" without actually making 15 HTTP calls.

How the fake Redis works
  We create an AsyncMock for the pipeline object and configure its
  execute() return value to be [removed_count, existing_count, added, ttl_set].
  The middleware reads results[1] (existing_count) to decide whether to rate limit.
  By changing that value we control the middleware's decision precisely.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.middleware.rate_limit import RATE_LIMIT, WINDOW_SECONDS


def make_redis_mock(existing_request_count: int) -> MagicMock:
    """
    Build a fake Redis client whose pipeline reports `existing_request_count`
    existing requests in the sliding window.

    The middleware reads results[1] from pipe.execute() — that's the ZCARD
    result (count before adding the current request). We set it here to
    simulate however many requests have already been made.

    Args:
        existing_request_count: How many requests the fake Redis reports
                                 as already in the current window.
    """
    # Build the pipeline mock — the object returned by redis.pipeline()
    pipe_mock = MagicMock()
    pipe_mock.zremrangebyscore = MagicMock(
        return_value=pipe_mock
    )  # returns self for chaining
    pipe_mock.zcard = MagicMock(return_value=pipe_mock)
    pipe_mock.zadd = MagicMock(return_value=pipe_mock)
    pipe_mock.expire = MagicMock(return_value=pipe_mock)

    # execute() is async and returns the results list.
    # results[0] = number removed (zremrangebyscore) — we don't care about this
    # results[1] = existing count (zcard) — THIS is what the middleware checks
    # results[2] = 1 (zadd added successfully)
    # results[3] = 1 (expire set successfully)
    pipe_mock.execute = AsyncMock(return_value=[0, existing_request_count, 1, 1])

    # Build the Redis client mock that returns our pipeline
    redis_mock = MagicMock()
    redis_mock.pipeline = MagicMock(return_value=pipe_mock)
    # zremrangebyscore is called directly (not via pipeline) when cleaning up
    # a rejected request — make it a no-op async function
    redis_mock.zremrangebyscore = AsyncMock(return_value=0)
    # aclose() is called by the lifespan on shutdown — must be awaitable
    # or teardown raises "object MagicMock can't be used in 'await' expression"
    redis_mock.aclose = AsyncMock()

    return redis_mock


@pytest.fixture
def client_with_redis(mock_redis_lifespan: MagicMock) -> TestClient:
    """
    A test client whose app.state.redis is a controlled fake Redis.

    We use the mock_redis_lifespan fixture (from conftest.py) which already
    patches aioredis.from_url. Here we additionally override app.state.redis
    directly so the middleware (which reads app.state.redis) also gets our mock.

    The fixture is parameterised per test via the `redis_mock` argument —
    each test creates a fresh make_redis_mock() with the count it needs.
    """
    with TestClient(app) as client:
        yield client


class TestRateLimitHeaders:
    """Verify that rate limit headers are present on normal (allowed) requests."""

    def test_allowed_request_has_rate_limit_headers(
        self, client_with_redis: TestClient
    ) -> None:
        """
        A request well within the limit should return 200 and include all
        three rate limit headers so clients can track their usage.
        """
        # Set up: 5 existing requests (well under the 20 limit)
        client_with_redis.app.state.redis = make_redis_mock(existing_request_count=5)  # type: ignore[attr-defined]

        response = client_with_redis.get(
            "/health"
        )  # /health is excluded — use root instead
        # Note: /health is excluded from rate limiting — we test via root
        response = client_with_redis.get("/")

        assert response.status_code == 200
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers
        assert response.headers["X-RateLimit-Limit"] == str(RATE_LIMIT)

    def test_remaining_count_decreases_as_limit_approaches(
        self, client_with_redis: TestClient
    ) -> None:
        """
        X-RateLimit-Remaining should reflect how many requests are left
        in the current window.
        """
        # 18 existing requests → 1 remaining after this one (20 - 18 - 1 = 1)
        client_with_redis.app.state.redis = make_redis_mock(existing_request_count=18)  # type: ignore[attr-defined]

        response = client_with_redis.get("/")

        assert response.status_code == 200
        assert response.headers["X-RateLimit-Remaining"] == "1"


class TestRateLimitEnforcement:
    """Verify that the 429 response is correct when the limit is exceeded."""

    def test_request_over_limit_returns_429(
        self, client_with_redis: TestClient
    ) -> None:
        """
        When existing_count >= RATE_LIMIT (20), the middleware must return 429.
        This simulates the 21st request arriving when 20 are already in the window.
        """
        # 20 existing requests = exactly at the limit → next one is rejected
        client_with_redis.app.state.redis = make_redis_mock(
            existing_request_count=RATE_LIMIT
        )  # type: ignore[attr-defined]

        response = client_with_redis.get("/")

        assert response.status_code == 429

    def test_429_body_has_correct_fields(self, client_with_redis: TestClient) -> None:
        """
        The 429 response body must contain error and retry_after_seconds
        so clients know what happened and when to try again.
        """
        client_with_redis.app.state.redis = make_redis_mock(
            existing_request_count=RATE_LIMIT
        )  # type: ignore[attr-defined]

        response = client_with_redis.get("/")
        body = response.json()

        assert body["error"] == "rate_limit_exceeded"
        assert body["retry_after_seconds"] == WINDOW_SECONDS

    def test_429_has_retry_after_header(self, client_with_redis: TestClient) -> None:
        """
        The standard Retry-After header tells HTTP clients and proxies
        how long to wait before retrying.
        """
        client_with_redis.app.state.redis = make_redis_mock(
            existing_request_count=RATE_LIMIT
        )  # type: ignore[attr-defined]

        response = client_with_redis.get("/")

        assert "Retry-After" in response.headers
        assert response.headers["X-RateLimit-Remaining"] == "0"

    def test_exactly_at_limit_is_rejected(self, client_with_redis: TestClient) -> None:
        """
        Boundary test: RATE_LIMIT existing requests → rejected (not allowed).
        RATE_LIMIT - 1 existing requests → allowed.
        """
        # RATE_LIMIT - 1 existing → this is the 20th request → allowed
        client_with_redis.app.state.redis = make_redis_mock(
            existing_request_count=RATE_LIMIT - 1
        )  # type: ignore[attr-defined]
        response = client_with_redis.get("/")
        assert response.status_code == 200

        # RATE_LIMIT existing → this is the 21st request → rejected
        client_with_redis.app.state.redis = make_redis_mock(
            existing_request_count=RATE_LIMIT
        )  # type: ignore[attr-defined]
        response = client_with_redis.get("/")
        assert response.status_code == 429


class TestRateLimitExclusions:
    """Verify that /health is never blocked, regardless of the request count."""

    def test_health_endpoint_not_rate_limited(
        self, client_with_redis: TestClient
    ) -> None:
        """
        /health must never return 429, even when the rate limit is exceeded.
        Monitoring tools hit this endpoint every few minutes — blocking them
        would cause false "service down" alerts.

        Note: we assert != 429 rather than == 200 because in the unit test
        environment the health check itself returns 503 (no real DB/Redis to ping).
        The critical property we're testing is that the rate limiter does NOT
        interfere — the response code is determined by the health check, not by
        rate limiting.
        """
        # Set the Redis mock to report we're way over the limit
        client_with_redis.app.state.redis = make_redis_mock(existing_request_count=999)  # type: ignore[attr-defined]

        response = client_with_redis.get("/health")

        # /health is excluded — must never be a rate limit rejection
        assert response.status_code != 429

    def test_health_endpoint_has_no_rate_limit_headers(
        self, client_with_redis: TestClient
    ) -> None:
        """
        Excluded paths skip the middleware entirely — they should NOT have
        rate limit headers because the middleware never ran for them.
        """
        client_with_redis.app.state.redis = make_redis_mock(existing_request_count=0)  # type: ignore[attr-defined]

        response = client_with_redis.get("/health")

        assert "X-RateLimit-Remaining" not in response.headers


class TestRateLimitFailOpen:
    """Verify that a Redis error does not block requests (fail open)."""

    def test_redis_error_allows_request(self, client_with_redis: TestClient) -> None:
        """
        If Redis is unavailable, the middleware must fail open — let the request
        through rather than block everyone. A broken rate limiter is much less bad
        than a broken API.
        """
        # Build a Redis mock whose pipeline.execute() raises an exception
        pipe_mock = MagicMock()
        pipe_mock.zremrangebyscore = MagicMock(return_value=pipe_mock)
        pipe_mock.zcard = MagicMock(return_value=pipe_mock)
        pipe_mock.zadd = MagicMock(return_value=pipe_mock)
        pipe_mock.expire = MagicMock(return_value=pipe_mock)
        pipe_mock.execute = AsyncMock(side_effect=ConnectionError("Redis is down"))

        broken_redis = MagicMock()
        broken_redis.pipeline = MagicMock(return_value=pipe_mock)
        broken_redis.aclose = AsyncMock()  # lifespan calls this on shutdown

        client_with_redis.app.state.redis = broken_redis  # type: ignore[attr-defined]

        # Should still return 200 — fail open
        response = client_with_redis.get("/")
        assert response.status_code == 200
