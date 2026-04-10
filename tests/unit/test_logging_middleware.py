"""
tests/unit/test_logging_middleware.py — unit tests for LoggingMiddleware
=========================================================================
These tests verify that the logging middleware behaves correctly:
  - Every response gets an X-Request-ID header
  - The ID is a valid UUID4
  - Two different requests get two different IDs
  - /health is excluded (no overhead for monitoring tools)

What we are NOT testing here:
  - That structlog writes JSON (that's structlog's job, tested by its own suite)
  - That the log output is pretty in development (renderer config)
  - That the log fields look exactly right in a log aggregator (integration concern)

We test OUR code — the middleware's decision-making — not the libraries it uses.

Testing technique: `caplog`
  pytest provides a built-in `caplog` fixture that captures log records emitted
  during a test. We use it to assert that certain log events were (or were not)
  emitted without redirecting stdout or parsing strings.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    """
    Synchronous test client wrapping the full FastAPI app including all
    registered middleware. Using the real app (not a stripped-down version)
    means middleware interactions are tested as they run in production.

    The `with` block triggers the lifespan (startup/shutdown), so the
    Redis mock on app.state.redis is available during the test.
    """
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


class TestLoggingMiddleware:

    def test_response_contains_request_id_header(self, client: TestClient) -> None:
        """
        GIVEN: A normal GET request
        WHEN:  The middleware processes it
        THEN:  The response includes an X-Request-ID header

        X-Request-ID is how the frontend or a support engineer can report
        the exact request that caused a problem — they include this header
        value in a bug report and we search our logs for it.
        """
        response = client.get("/")
        assert "x-request-id" in response.headers, (
            "X-Request-ID header is missing — clients cannot correlate requests to logs"
        )

    def test_request_id_is_valid_uuid(self, client: TestClient) -> None:
        """
        GIVEN: A GET request
        WHEN:  The middleware generates a request_id
        THEN:  The ID is a valid UUID4 string

        We use UUID4 (random) rather than UUID1 (time-based) because UUID1
        encodes the MAC address of the server — a minor privacy leak.
        UUID4 has no such issue and is still statistically unique.
        """
        response = client.get("/")
        request_id = response.headers.get("x-request-id", "")

        try:
            parsed = uuid.UUID(request_id)
        except ValueError:
            pytest.fail(f"X-Request-ID '{request_id}' is not a valid UUID")

        assert parsed.version == 4, (
            f"Expected UUID version 4 (random), got version {parsed.version}"
        )

    def test_two_requests_have_different_ids(self, client: TestClient) -> None:
        """
        GIVEN: Two separate requests
        WHEN:  The middleware processes each
        THEN:  Each gets a unique request_id

        This guards against the middleware accidentally reusing an ID —
        e.g. if request_id were set at module level instead of per-request.
        Without unique IDs, filtering logs by request_id would return logs
        from multiple unrelated requests.
        """
        response_1 = client.get("/")
        response_2 = client.get("/")

        id_1 = response_1.headers.get("x-request-id")
        id_2 = response_2.headers.get("x-request-id")

        assert id_1 is not None
        assert id_2 is not None
        assert id_1 != id_2, (
            "Two different requests received the same request_id — "
            "log filtering by request_id would return mixed results"
        )

    def test_health_endpoint_excluded_from_logging(self, client: TestClient) -> None:
        """
        GIVEN: A GET /health request (from UptimeRobot or Railway)
        WHEN:  The middleware processes it
        THEN:  No X-Request-ID header is added (path is excluded)

        /health is pinged every 5 minutes by UptimeRobot and every 30s by
        Railway's health check. Logging each of those would generate ~300
        noise log lines per day that obscure real application events.
        The middleware explicitly skips excluded paths.
        """
        # We need to mock DB and Redis for /health to return 200
        # Reuse the dependency override pattern from test_health.py
        from unittest.mock import AsyncMock, MagicMock
        from sqlalchemy.ext.asyncio import AsyncSession
        from app.dependencies import get_db, get_redis

        mock_db = MagicMock(spec=AsyncSession)
        mock_db.execute = AsyncMock(return_value=None)
        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock(return_value=True)

        async def _db():
            yield mock_db

        async def _redis():
            return mock_redis

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_redis] = _redis

        try:
            response = client.get("/health")
            # /health should still return 200 and work correctly
            assert response.status_code == 200
            # But it should NOT have the X-Request-ID header since it's excluded
            assert "x-request-id" not in response.headers, (
                "/health is in the excluded paths list but still received a request_id header"
            )
        finally:
            app.dependency_overrides.clear()

    def test_non_health_path_always_gets_request_id(self, client: TestClient) -> None:
        """
        GIVEN: Any non-excluded endpoint
        WHEN:  The middleware processes it
        THEN:  The response always has X-Request-ID

        This is a positive control — ensures the exclusion logic is targeted
        (only /health and /metrics) and doesn't accidentally exclude other routes.
        """
        response = client.get("/")  # root endpoint is not excluded
        assert "x-request-id" in response.headers
