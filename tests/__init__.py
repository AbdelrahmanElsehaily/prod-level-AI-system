"""
tests/
======
Test suite for the Chat API.

Structure mirrors the app layout:
  tests/unit/        — fast tests, no external services, everything mocked
  tests/integration/ — slower tests that hit real Postgres and Redis
                       (run by CI against service containers)

Run only unit tests locally:
    pytest tests/unit/

Run everything (requires Docker services running):
    pytest tests/
"""
