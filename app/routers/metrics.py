"""
app/routers/metrics.py — GET /metrics
======================================
Returns operational counters for the running process. Protected by a
secret token so it is not publicly readable.

Authentication
--------------
The caller must include the header:
    X-Metrics-Token: <value of METRICS_TOKEN env var>

Returns 403 if the header is missing or wrong. This is intentionally
simple — metrics data is not sensitive enough to warrant OAuth, but we
don't want it publicly indexed either.

Why a custom header and not Basic Auth?
  Basic Auth requires the client to base64-encode credentials and set an
  Authorization header. A static token in a custom header is simpler to
  configure in UptimeRobot, Grafana, and curl one-liners, and equally
  secure for this use case.

Why not just make /metrics public?
  Request rates, error counts, and token costs reveal information about
  traffic patterns and infrastructure costs. A secret header adds a
  trivial amount of protection without complicating the implementation.
"""

import structlog
from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import JSONResponse

from app.config import settings
from app.metrics import metrics

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get(
    "/metrics",
    summary="Operational metrics",
    description=(
        "Returns in-memory counters for the running process. "
        "Requires `X-Metrics-Token` header matching `METRICS_TOKEN` env var."
    ),
)
async def get_metrics(
    x_metrics_token: str | None = Header(default=None),
) -> JSONResponse:
    """
    Return operational counters. Returns 403 if the token is wrong or missing.
    """
    if not settings.metrics_token:
        # If METRICS_TOKEN is not configured, the endpoint is disabled entirely.
        # This prevents accidentally exposing metrics in environments where the
        # env var was forgotten.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Metrics endpoint is disabled — METRICS_TOKEN not configured.",
        )

    if x_metrics_token != settings.metrics_token:
        await logger.awarning(
            "metrics auth failed",
            provided_token=x_metrics_token[:4] + "…" if x_metrics_token else None,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-Metrics-Token header.",
        )

    return JSONResponse(
        content={
            "uptime_seconds": metrics.uptime_seconds,
            "requests_total": metrics.requests_total,
            "requests_per_minute": metrics.requests_per_minute,
            "ai_calls_total": metrics.ai_calls_total,
            "ai_tokens_total": metrics.ai_tokens_total,
            "ai_estimated_cost_usd": round(metrics.ai_estimated_cost_usd, 6),
            "errors_total": metrics.errors_total,
            "avg_response_time_ms": metrics.avg_response_time_ms,
        }
    )
