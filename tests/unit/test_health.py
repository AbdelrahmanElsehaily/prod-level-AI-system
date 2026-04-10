"""
tests/unit/test_health.py — unit tests for GET /health
========================================================
These tests verify the health endpoint's behaviour WITHOUT connecting to any
real external service. We replace the database and Redis dependencies with
simple async mock functions using FastAPI's dependency_overrides system.

How dependency_overrides works:
  app.dependency_overrides[original_function] = replacement_function

  When FastAPI processes a request and encounters Depends(get_db), it checks
  if get_db is in dependency_overrides. If it is, it calls the replacement
  instead. The route function never knows the difference — it just receives
  whatever the dependency function returns.

  This is the correct, framework-supported way to mock dependencies in FastAPI.
  It is better than patching with unittest.mock because:
    - It works at the FastAPI routing level (not Python's import system)
    - It composes cleanly — multiple overrides can stack
    - It is guaranteed to be reset after the test if you use a fixture
"""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_redis
from app.main import app


# ---------------------------------------------------------------------------
# Mock factories
# ---------------------------------------------------------------------------

def make_mock_db(raises: Exception | None = None) -> AsyncSession:
    """
    Build a mock AsyncSession that either:
      - succeeds silently (raises=None), or
      - raises the given exception when .execute() is called.

    We use MagicMock with spec=AsyncSession so that attribute access on the
    mock is validated against the real AsyncSession interface. This prevents
    typos in the mock setup from going undetected.
    """
    mock = MagicMock(spec=AsyncSession)
    if raises:
        mock.execute = AsyncMock(side_effect=raises)
    else:
        mock.execute = AsyncMock(return_value=None)
    return mock


def make_mock_redis(raises: Exception | None = None) -> MagicMock:
    """
    Build a mock Redis client that either succeeds or raises on .ping().
    """
    mock = MagicMock()
    if raises:
        mock.ping = AsyncMock(side_effect=raises)
    else:
        mock.ping = AsyncMock(return_value=True)
    return mock


# ---------------------------------------------------------------------------
# Helper: apply dependency overrides for a test
# ---------------------------------------------------------------------------
# We write a small context manager so each test sets up and tears down
# overrides cleanly without repetition.

def override_deps(db_mock: AsyncSession, redis_mock: MagicMock) -> None:
    """Install dependency overrides on the global app."""

    async def _db() -> AsyncGenerator[AsyncSession, None]:
        yield db_mock

    async def _redis() -> MagicMock:
        return redis_mock

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = _redis


def clear_overrides() -> None:
    """Remove all dependency overrides — always call this after each test."""
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    """
    Group related tests in a class to keep the file organised.
    pytest discovers test classes automatically (no inheritance needed).
    """

    def test_healthy_when_db_and_redis_ok(self, test_client: TestClient) -> None:
        """
        GIVEN: Postgres and Redis both respond successfully
        WHEN:  GET /health is called
        THEN:  Response is 200 with status="healthy" and both checks showing "ok"

        The GIVEN/WHEN/THEN (or Arrange/Act/Assert) pattern makes the
        intent of each test immediately clear.
        """
        override_deps(make_mock_db(), make_mock_redis())
        try:
            response = test_client.get("/health")
            data = response.json()

            assert response.status_code == 200
            assert data["status"] == "healthy"
            assert data["checks"]["database"] == "ok"
            assert data["checks"]["redis"] == "ok"
            # Verify the response includes the expected keys
            assert "version" in data
            assert "environment" in data
            assert "response_time_ms" in data
        finally:
            # Always clean up overrides — if we don't, the mock bleeds into
            # other tests, causing mysterious failures.
            clear_overrides()

    def test_degraded_when_db_fails(self, test_client: TestClient) -> None:
        """
        GIVEN: Postgres raises a connection error
        WHEN:  GET /health is called
        THEN:  Response is 503 with status="degraded" and database check showing error
        """
        db_error = ConnectionRefusedError("could not connect to server")
        override_deps(make_mock_db(raises=db_error), make_mock_redis())
        try:
            response = test_client.get("/health")
            data = response.json()

            assert response.status_code == 503
            assert data["status"] == "degraded"
            # The error message should be surfaced in the check result
            assert "error" in data["checks"]["database"]
            # Redis was fine, so it should still show ok
            assert data["checks"]["redis"] == "ok"
        finally:
            clear_overrides()

    def test_degraded_when_redis_fails(self, test_client: TestClient) -> None:
        """
        GIVEN: Redis raises a connection error
        WHEN:  GET /health is called
        THEN:  Response is 503 with redis check showing the error
        """
        redis_error = ConnectionRefusedError("Connection refused")
        override_deps(make_mock_db(), make_mock_redis(raises=redis_error))
        try:
            response = test_client.get("/health")
            data = response.json()

            assert response.status_code == 503
            assert data["status"] == "degraded"
            assert data["checks"]["database"] == "ok"
            assert "error" in data["checks"]["redis"]
        finally:
            clear_overrides()

    def test_degraded_when_both_fail(self, test_client: TestClient) -> None:
        """
        GIVEN: Both Postgres and Redis raise errors
        WHEN:  GET /health is called
        THEN:  Response is 503 and both checks show errors
        """
        override_deps(
            make_mock_db(raises=Exception("db down")),
            make_mock_redis(raises=Exception("redis down")),
        )
        try:
            response = test_client.get("/health")
            data = response.json()

            assert response.status_code == 503
            assert data["status"] == "degraded"
            assert "error" in data["checks"]["database"]
            assert "error" in data["checks"]["redis"]
        finally:
            clear_overrides()

    def test_response_shape(self, test_client: TestClient) -> None:
        """
        GIVEN: Services are healthy
        WHEN:  GET /health is called
        THEN:  Response body contains exactly the fields we expect

        This test acts as a contract — if someone changes the response shape,
        this test fails and they know they need to update clients/monitors too.
        """
        override_deps(make_mock_db(), make_mock_redis())
        try:
            response = test_client.get("/health")
            data = response.json()

            required_keys = {"status", "environment", "version", "checks", "response_time_ms"}
            assert required_keys.issubset(data.keys()), (
                f"Missing keys in response: {required_keys - data.keys()}"
            )
            required_check_keys = {"database", "redis"}
            assert required_check_keys.issubset(data["checks"].keys())
        finally:
            clear_overrides()
