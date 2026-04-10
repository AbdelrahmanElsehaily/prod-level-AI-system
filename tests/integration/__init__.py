"""
tests/integration/
==================
Integration tests: use real Postgres and Redis (via Docker or CI service containers).
The Anthropic API is still mocked — we never call real AI APIs in tests.

Run these tests with services running:
    docker compose up -d postgres redis
    pytest tests/integration/
"""
