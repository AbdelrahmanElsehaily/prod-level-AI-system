"""
tests/unit/test_metrics.py — unit tests for GET /metrics
=========================================================
Tests cover:
  - 403 when METRICS_TOKEN is not configured
  - 403 when wrong token is supplied
  - 200 with correct token, correct response shape
  - All expected fields present
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


class TestMetricsAuth:
    def test_missing_token_returns_403(self) -> None:
        """No X-Metrics-Token header → 403."""
        with patch("app.routers.metrics.settings") as mock_settings:
            mock_settings.metrics_token = "secret-token"
            resp = client.get("/metrics")
        assert resp.status_code == 403

    def test_wrong_token_returns_403(self) -> None:
        """Wrong X-Metrics-Token value → 403."""
        with patch("app.routers.metrics.settings") as mock_settings:
            mock_settings.metrics_token = "secret-token"
            resp = client.get("/metrics", headers={"X-Metrics-Token": "wrong"})
        assert resp.status_code == 403

    def test_unconfigured_token_returns_403(self) -> None:
        """METRICS_TOKEN not set → 403 regardless of header."""
        with patch("app.routers.metrics.settings") as mock_settings:
            mock_settings.metrics_token = None
            resp = client.get("/metrics", headers={"X-Metrics-Token": "anything"})
        assert resp.status_code == 403

    def test_correct_token_returns_200(self) -> None:
        """Correct X-Metrics-Token → 200."""
        with patch("app.routers.metrics.settings") as mock_settings:
            mock_settings.metrics_token = "secret-token"
            resp = client.get("/metrics", headers={"X-Metrics-Token": "secret-token"})
        assert resp.status_code == 200


class TestMetricsShape:
    def test_all_expected_fields_present(self) -> None:
        """Response must contain all fields defined in the plan."""
        expected_fields = {
            "uptime_seconds",
            "requests_total",
            "requests_per_minute",
            "ai_calls_total",
            "ai_tokens_total",
            "ai_estimated_cost_usd",
            "errors_total",
            "avg_response_time_ms",
        }
        with patch("app.routers.metrics.settings") as mock_settings:
            mock_settings.metrics_token = "secret-token"
            resp = client.get("/metrics", headers={"X-Metrics-Token": "secret-token"})

        assert resp.status_code == 200
        data = resp.json()
        assert expected_fields.issubset(data.keys())

    def test_counters_are_numeric(self) -> None:
        """All counter values must be int or float, never None or string."""
        with patch("app.routers.metrics.settings") as mock_settings:
            mock_settings.metrics_token = "tok"
            resp = client.get("/metrics", headers={"X-Metrics-Token": "tok"})

        data = resp.json()
        for key, value in data.items():
            assert isinstance(value, (int, float)), (
                f"{key} should be numeric, got {type(value)}"
            )


class TestMetricsCounters:
    def test_record_request_increments_counter(self) -> None:
        from app.metrics import Metrics

        m = Metrics()
        assert m.requests_total == 0
        m.record_request(duration_ms=50.0)
        assert m.requests_total == 1

    def test_avg_response_time_calculated_correctly(self) -> None:
        from app.metrics import Metrics

        m = Metrics()
        m.record_request(duration_ms=100.0)
        m.record_request(duration_ms=200.0)
        assert m.avg_response_time_ms == 150.0

    def test_avg_response_time_zero_when_no_requests(self) -> None:
        from app.metrics import Metrics

        m = Metrics()
        assert m.avg_response_time_ms == 0.0

    def test_record_ai_call_increments_counters(self) -> None:
        from app.metrics import Metrics

        m = Metrics()
        m.record_ai_call(tokens=100, cost_usd=0.000006)
        m.record_ai_call(tokens=200, cost_usd=0.000012)
        assert m.ai_calls_total == 2
        assert m.ai_tokens_total == 300
        assert round(m.ai_estimated_cost_usd, 6) == 0.000018

    def test_record_error_increments_counter(self) -> None:
        from app.metrics import Metrics

        m = Metrics()
        m.record_error()
        m.record_error()
        assert m.errors_total == 2

    def test_uptime_is_positive(self) -> None:
        from app.metrics import Metrics

        m = Metrics()
        assert m.uptime_seconds >= 0
