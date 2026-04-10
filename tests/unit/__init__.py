"""
tests/unit/
===========
Unit tests: no network calls, no database, no Redis.
All external dependencies are replaced with mocks/fakes via
FastAPI's dependency_overrides system.

Each test should complete in under 10ms.
"""
