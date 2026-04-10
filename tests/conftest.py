"""
tests/conftest.py — shared pytest fixtures
==========================================
conftest.py is a special pytest file. Any fixtures defined here are
automatically available to ALL tests in this directory and subdirectories
without needing to import them.

What's a fixture?
  A fixture is a function decorated with @pytest.fixture that sets up
  (and optionally tears down) a resource for tests.

  Example — a fixture that provides a clean HTTP test client:
    @pytest.fixture
    def client(app): ...

  A test then declares it needs that fixture by listing its name as a parameter:
    def test_something(client):  # pytest injects the fixture value here
        response = client.get("/health")

Shared fixtures (defined here, in the root conftest) vs local fixtures
(defined in a test file) follow a simple rule:
  If multiple test files need the same fixture → put it here.
  If only one file needs it → define it in that file.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def test_client() -> TestClient:
    """
    A synchronous HTTP test client for the FastAPI app.

    httpx.TestClient (wrapped by FastAPI's TestClient) lets you make HTTP
    requests to your app in tests WITHOUT starting a real server.
    Requests go directly to the ASGI app in-process, making tests fast.

    Note: we use the synchronous TestClient (not AsyncClient) here because
    it's simpler for unit tests. Integration tests may use AsyncClient.
    """
    # TestClient runs the lifespan (startup/shutdown) on enter/exit.
    # Using it as a context manager ensures the Redis connection from
    # the lifespan startup is properly closed after the test.
    with TestClient(app) as client:
        yield client
