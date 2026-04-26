"""
tests/unit/test_metrics.py — unit tests for GET /metrics
=========================================================
Tests cover:
  - 403 when METRICS_TOKEN is not configured
  - 403 when wrong token is supplied
  - 200 with correct token, returns Prometheus text format
  - Counter primitives increment correctly
  - Histogram primitive observes durations
"""

from unittest.mock import patch

from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST

from app.main import app
from app.metrics import (
    ai_calls_total,
    ai_cost_usd_total,
    ai_errors_total,
    ai_tokens_total,
    http_request_duration_seconds,
    http_requests_total,
)

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


class TestMetricsExposition:
    def test_response_is_prometheus_text_format(self) -> None:
        """Content-Type must match the Prometheus exposition format."""
        with patch("app.routers.metrics.settings") as mock_settings:
            mock_settings.metrics_token = "tok"
            resp = client.get("/metrics", headers={"X-Metrics-Token": "tok"})

        assert resp.status_code == 200
        # CONTENT_TYPE_LATEST is the canonical content type Prometheus
        # scrapers expect: "text/plain; version=0.0.4; charset=utf-8".
        assert resp.headers["content-type"].startswith(
            CONTENT_TYPE_LATEST.split(";")[0]
        )

    def test_expected_metrics_are_present(self) -> None:
        """All custom metric names must appear in the exposition body."""
        # Touch each metric so it shows up in the registry output even on a
        # fresh process (counters with no observations are still listed by
        # prometheus-client, but explicit labels guarantee they render).
        http_requests_total.labels(method="GET", path="/x", status="200")
        http_request_duration_seconds.labels(method="GET", path="/x")
        ai_calls_total.labels(model="m")
        ai_tokens_total.labels(model="m")
        ai_cost_usd_total.labels(model="m")
        ai_errors_total.labels(model="m", kind="k")

        with patch("app.routers.metrics.settings") as mock_settings:
            mock_settings.metrics_token = "tok"
            resp = client.get("/metrics", headers={"X-Metrics-Token": "tok"})

        body = resp.text
        for name in (
            "http_requests_total",
            "http_request_duration_seconds",
            "ai_calls_total",
            "ai_tokens_total",
            "ai_cost_usd_total",
            "ai_errors_total",
            "app_start_time_seconds",
        ):
            assert name in body, f"{name} missing from /metrics output"


class TestMetricCounters:
    """Verify our prometheus-client primitives behave the way we expect."""

    def test_counter_increments(self) -> None:
        c = http_requests_total.labels(method="GET", path="/_unit_test_a", status="200")
        before = c._value.get()
        c.inc()
        assert c._value.get() == before + 1

    def test_counter_inc_with_amount(self) -> None:
        c = ai_tokens_total.labels(model="_unit_test")
        before = c._value.get()
        c.inc(123)
        assert c._value.get() == before + 123

    def test_histogram_observe(self) -> None:
        h = http_request_duration_seconds.labels(method="GET", path="/_unit_test_b")
        before_count = h._sum.get()
        h.observe(0.123)
        assert h._sum.get() == before_count + 0.123

    def test_float_counter_for_cost(self) -> None:
        c = ai_cost_usd_total.labels(model="_unit_test_cost")
        before = c._value.get()
        c.inc(0.000018)
        assert round(c._value.get() - before, 6) == 0.000018
