"""
app/routers/metrics.py — GET /metrics (Prometheus exposition)
=============================================================
Exposes app metrics in the Prometheus text exposition format. Grafana
Cloud's hosted scraper is configured to GET this URL on a schedule
(default 60s) with the X-Metrics-Token header attached, parses the body,
and stores the time series in its managed Prometheus database.

Authentication
--------------
The caller must include:
    X-Metrics-Token: <value of METRICS_TOKEN env var>

Returns 403 if the header is missing or wrong, OR if METRICS_TOKEN is
not configured at all (fail-closed: an unset token disables the endpoint
rather than leaving it open).

Response format
---------------
Standard Prometheus text exposition (version 0.0.4):

    # HELP http_requests_total Total HTTP requests handled by the app.
    # TYPE http_requests_total counter
    http_requests_total{method="GET",path="/health",status="200"} 42.0
    ...

Content-Type MUST be `text/plain; version=0.0.4; charset=utf-8` — this
is what Prometheus scrapers (and Grafana Cloud's hosted agent) expect.
Returning JSON or omitting the version parameter causes silent scrape
failures with no useful error.
"""

import structlog
from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import settings

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get(
    "/metrics",
    summary="Prometheus metrics exposition",
    description=(
        "Returns app metrics in Prometheus text format. "
        "Requires `X-Metrics-Token` header matching `METRICS_TOKEN` env var. "
        "Scraped by Grafana Cloud."
    ),
)
async def get_metrics(
    x_metrics_token: str | None = Header(default=None),
) -> Response:
    """Return the Prometheus exposition for all registered metrics."""
    if not settings.metrics_token:
        # Fail-closed: if METRICS_TOKEN is not configured, the endpoint is
        # disabled entirely. Prevents accidentally exposing metrics in an
        # environment where the env var was forgotten.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Metrics endpoint is disabled — METRICS_TOKEN not configured.",
        )

    if x_metrics_token != settings.metrics_token:
        await logger.awarning(
            "metrics auth failed",
            provided_token=(x_metrics_token[:4] + "…") if x_metrics_token else None,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-Metrics-Token header.",
        )

    # generate_latest() walks the global registry and renders every
    # Counter/Histogram/Gauge as Prometheus text. CONTENT_TYPE_LATEST is
    # the exact Content-Type string the Prometheus scraper expects.
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
