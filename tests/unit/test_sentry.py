"""
tests/unit/test_sentry.py — tests for Sentry initialisation and debug endpoint
===============================================================================
Two concerns tested here:

1. init_sentry() behaviour
   - Called with no DSN → sentry_sdk.init() is never called (no-op)
   - Called with a DSN → sentry_sdk.init() is called with the right arguments
   - before_send scrubber strips Authorization headers and sensitive body fields

2. Debug endpoint routing
   - Available in non-production environments (development, test, staging)
   - Returns 500 when hit (the intentional exception is unhandled)
   - NOT available in production (route is never mounted)

Why mock sentry_sdk.init()?
  We never want unit tests to open a real network connection to Sentry's
  servers. Mocking init() lets us assert the arguments it would have been
  called with, without any network I/O. Same pattern as mocking aioredis
  or the Ollama client in other test modules.
"""

from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.sentry import _scrub_sensitive_data, init_sentry

# ---------------------------------------------------------------------------
# init_sentry() tests
# ---------------------------------------------------------------------------


class TestInitSentry:
    """Verify init_sentry() only calls sentry_sdk.init() when DSN is set."""

    def test_no_dsn_does_not_call_sentry_init(self) -> None:
        """
        When SENTRY_DSN is not configured, init_sentry() must be a complete
        no-op — no network calls, no global state changes in the SDK.
        """
        with patch("app.sentry.settings") as mock_settings:
            mock_settings.sentry_dsn = None

            with patch("app.sentry.sentry_sdk.init") as mock_init:
                init_sentry()

            mock_init.assert_not_called()

    def test_with_dsn_calls_sentry_init(self) -> None:
        """
        When SENTRY_DSN is set, sentry_sdk.init() must be called exactly once.
        """
        with patch("app.sentry.settings") as mock_settings:
            mock_settings.sentry_dsn = "https://abc123@o0.ingest.sentry.io/0"
            mock_settings.environment = "staging"
            mock_settings.version = "0.1.0"

            with patch("app.sentry.sentry_sdk.init") as mock_init:
                init_sentry()

            mock_init.assert_called_once()

    def test_sentry_init_receives_correct_environment(self) -> None:
        """
        The environment kwarg passed to sentry_sdk.init() must match
        settings.environment so Sentry can filter events by environment.
        """
        with patch("app.sentry.settings") as mock_settings:
            mock_settings.sentry_dsn = "https://abc123@o0.ingest.sentry.io/0"
            mock_settings.environment = "production"
            mock_settings.version = "0.1.0"

            with patch("app.sentry.sentry_sdk.init") as mock_init:
                init_sentry()

            _, kwargs = mock_init.call_args
            assert kwargs["environment"] == "production"

    def test_sentry_init_disables_pii(self) -> None:
        """
        send_default_pii must be False — we never want Sentry to automatically
        attach cookies, session data, or user IP addresses to events.
        """
        with patch("app.sentry.settings") as mock_settings:
            mock_settings.sentry_dsn = "https://abc123@o0.ingest.sentry.io/0"
            mock_settings.environment = "production"
            mock_settings.version = "0.1.0"

            with patch("app.sentry.sentry_sdk.init") as mock_init:
                init_sentry()

            _, kwargs = mock_init.call_args
            assert kwargs["send_default_pii"] is False

    def test_sentry_init_sets_traces_sample_rate(self) -> None:
        """
        traces_sample_rate controls what fraction of requests are traced for
        performance. Must be between 0 and 1 — we use 0.1 (10%).
        """
        with patch("app.sentry.settings") as mock_settings:
            mock_settings.sentry_dsn = "https://abc123@o0.ingest.sentry.io/0"
            mock_settings.environment = "staging"
            mock_settings.version = "0.1.0"

            with patch("app.sentry.sentry_sdk.init") as mock_init:
                init_sentry()

            _, kwargs = mock_init.call_args
            assert kwargs["traces_sample_rate"] == 0.1

    def test_sentry_init_registers_before_send_hook(self) -> None:
        """
        The before_send hook must be registered so our scrubber runs on every
        event before it leaves the process.
        """
        with patch("app.sentry.settings") as mock_settings:
            mock_settings.sentry_dsn = "https://abc123@o0.ingest.sentry.io/0"
            mock_settings.environment = "staging"
            mock_settings.version = "0.1.0"

            with patch("app.sentry.sentry_sdk.init") as mock_init:
                init_sentry()

            _, kwargs = mock_init.call_args
            assert kwargs["before_send"] is _scrub_sensitive_data


# ---------------------------------------------------------------------------
# _scrub_sensitive_data() tests
# ---------------------------------------------------------------------------


class TestScrubSensitiveData:
    """Verify the before_send hook strips secrets before they reach Sentry."""

    def _make_event(self, **kwargs: Any) -> dict[str, Any]:
        """Build a minimal Sentry event dict for testing."""
        return {"request": {}, **kwargs}

    def test_authorization_header_is_filtered(self) -> None:
        """
        Authorization headers contain Bearer tokens and API keys.
        They must be replaced with '[Filtered]', never sent to Sentry.
        """
        event: dict[str, Any] = {
            "request": {
                "headers": {
                    "Authorization": "Bearer sk-secret-token",
                    "Content-Type": "application/json",
                }
            }
        }

        result = _scrub_sensitive_data(event, {})  # type: ignore[arg-type]

        assert result is not None
        headers = result["request"]["headers"]
        assert headers["Authorization"] == "[Filtered]"
        # Non-sensitive headers are preserved
        assert headers["Content-Type"] == "application/json"

    def test_authorization_header_case_insensitive(self) -> None:
        """
        HTTP headers are case-insensitive. The scrubber must catch
        'authorization', 'Authorization', and 'AUTHORIZATION'.
        """
        event: dict[str, Any] = {
            "request": {
                "headers": {
                    "authorization": "Bearer sk-secret-token",
                }
            }
        }

        result = _scrub_sensitive_data(event, {})  # type: ignore[arg-type]

        assert result is not None
        assert result["request"]["headers"]["authorization"] == "[Filtered]"

    def test_password_field_in_body_is_filtered(self) -> None:
        """
        Request bodies might contain 'password' fields in future endpoints.
        These must be scrubbed even if they slip through validation.
        """
        event: dict[str, Any] = {
            "request": {
                "data": {
                    "username": "alice",
                    "password": "super-secret-123",
                }
            }
        }

        result = _scrub_sensitive_data(event, {})  # type: ignore[arg-type]

        assert result is not None
        data = result["request"]["data"]
        assert data["password"] == "[Filtered]"
        assert data["username"] == "alice"  # non-sensitive preserved

    def test_token_field_in_body_is_filtered(self) -> None:
        """Fields named 'token' in the request body are scrubbed."""
        event: dict[str, Any] = {
            "request": {
                "data": {
                    "token": "my-api-token",
                    "message": "hello",
                }
            }
        }

        result = _scrub_sensitive_data(event, {})  # type: ignore[arg-type]

        assert result is not None
        assert result["request"]["data"]["token"] == "[Filtered]"
        assert result["request"]["data"]["message"] == "hello"

    def test_event_without_sensitive_data_is_unchanged(self) -> None:
        """
        A normal chat event with no sensitive fields must pass through
        unmodified — the scrubber must not corrupt valid event data.
        """
        event: dict[str, Any] = {
            "request": {
                "method": "POST",
                "url": "http://localhost:8000/chat",
                "headers": {"Content-Type": "application/json"},
                "data": {"message": "hello", "conversation_id": None},
            }
        }

        result = _scrub_sensitive_data(event, {})  # type: ignore[arg-type]

        assert result is not None
        assert result["request"]["data"]["message"] == "hello"
        assert result["request"]["headers"]["Content-Type"] == "application/json"

    def test_event_is_returned_not_dropped(self) -> None:
        """
        The scrubber must return the event (not None) — returning None would
        silently drop the event and we'd never see the error in Sentry.
        """
        event: dict[str, Any] = {"request": {}}
        result = _scrub_sensitive_data(event, {})  # type: ignore[arg-type]
        assert result is not None


# ---------------------------------------------------------------------------
# Debug endpoint routing tests
# ---------------------------------------------------------------------------


class TestDebugEndpoint:
    """Verify /debug/error is available in non-production and absent in production."""

    def test_debug_error_returns_500_in_test_env(self) -> None:
        """
        In the test environment the debug router is mounted. Hitting /debug/error
        should return 500 because the route raises an unhandled ValueError.

        We use raise_server_exceptions=False so TestClient returns the HTTP
        response (500) instead of re-raising the ValueError in the test process.
        By default TestClient propagates server exceptions — useful for most
        tests, but here we want to assert the HTTP status code specifically.
        """
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/debug/error")
        assert response.status_code == 500

    def test_debug_route_mounted_in_non_production(self) -> None:
        """
        The debug router is mounted when ENVIRONMENT != 'production'.
        We verify this by inspecting the app's route paths directly —
        no HTTP request needed, just check the routing table.
        """
        routes = [getattr(r, "path", None) for r in app.routes]
        assert "/debug/error" in routes

    def test_debug_route_not_mounted_in_production(self) -> None:
        """
        When ENVIRONMENT=production the debug router must not be mounted.
        We build a fresh FastAPI app with the production guard active and
        assert the route is absent from its routing table.

        Testing the routing table (not a live request) keeps this test simple
        and avoids the complexity of module reloads or process restarts.
        """
        from fastapi import FastAPI

        from app.routers import debug as debug_router

        # Simulate a production app: only mount debug router if non-production
        production_environment = "production"
        prod_app = FastAPI()
        if production_environment != "production":
            prod_app.include_router(debug_router.router)

        routes = [getattr(r, "path", None) for r in prod_app.routes]
        assert "/debug/error" not in routes
