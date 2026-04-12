"""
tests/conftest.py — shared pytest fixtures and test environment setup
======================================================================
conftest.py is a special pytest file. Any fixtures defined here are
automatically available to ALL tests in this directory and subdirectories
without needing to import them.

Environment setup
-----------------
We set ENVIRONMENT=test before importing anything from the app.
This has two effects:

  1. run_migrations() in app/main.py returns early (skips Alembic).
     Why: migrations/env.py calls asyncio.run() internally. pytest-asyncio
     already has a running event loop, so asyncio.run() raises:
         RuntimeError: asyncio.run() cannot be called from a running event loop
     Unit tests mock the DB via dependency_overrides anyway — there is no
     real Postgres to migrate against.

  2. Logging uses console (pretty) format instead of JSON in unit tests,
     making test output readable in the terminal.

Why set it here and not in pytest.ini / pyproject.toml?
  [tool.pytest.ini_options] env = ["ENVIRONMENT=test"] requires the
  pytest-env plugin (an extra dependency). Setting it directly in conftest.py
  via os.environ works without any extra packages and is explicit.
"""

import os

# Must be set BEFORE any app module is imported, because app/config.py reads
# env vars at import time (the Settings() call at the bottom of config.py).
# Once Settings() has run, changing os.environ has no effect on `settings`.
os.environ.setdefault("ENVIRONMENT", "test")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def mock_redis_lifespan():
    """
    Patch aioredis.from_url for every unit test so the lifespan startup does
    not attempt a real TCP connection to Redis at localhost:6379.

    Why autouse=True?
      Every test that uses TestClient triggers the lifespan, which calls
      aioredis.from_url(). Without this patch, tests fail with a connection
      refused error if Redis isn't running locally — even though the test
      itself mocks Redis at the dependency level.

      autouse=True applies this fixture to every test in the suite automatically,
      without each test needing to declare it. It is the right choice for
      infrastructure-level patches that must always be active in unit tests.

    What this does NOT mock:
      Individual test cases that test Redis behaviour (e.g. rate limiting, caching)
      still override get_redis via dependency_overrides — that mock controls what
      the route handler receives. This fixture only prevents the lifespan from
      crashing before the test even starts.
    """
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()  # lifespan calls aclose() on shutdown
    mock_client.ping = AsyncMock(return_value=True)

    with patch("app.main.aioredis.from_url", return_value=mock_client):
        yield mock_client


@pytest.fixture
def test_client() -> TestClient:
    """
    A synchronous HTTP test client for the FastAPI app.

    httpx.TestClient lets you make HTTP requests to your app in tests WITHOUT
    starting a real server. Requests go directly to the ASGI app in-process.

    Using it as a context manager runs the lifespan (startup/shutdown):
      - Startup: setup_logging(), skip migrations (ENVIRONMENT=test),
                 store mock Redis on app.state (via mock_redis_lifespan fixture)
      - Shutdown: call aclose() on the mock Redis client
    """
    with TestClient(app) as client:
        yield client
