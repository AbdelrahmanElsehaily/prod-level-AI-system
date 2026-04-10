"""
app/routers/health.py — GET /health
=====================================
The health endpoint is the first thing every external system checks:
  - Railway / Kubernetes use it to decide if the deployment succeeded
  - UptimeRobot pings it every 5 minutes to detect outages
  - Load balancers use it to decide which instances should receive traffic

A health endpoint that simply returns 200 is worthless — it would pass even
if your database is completely down. This endpoint actually pings Postgres
and Redis. If either fails, it returns HTTP 503 (Service Unavailable) so the
platform/monitor knows something is wrong.

HTTP status code semantics:
  200 — everything is up, send traffic here
  503 — something is wrong, do NOT send traffic here (or alert the on-call)
"""

import time

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import get_db, get_redis

# APIRouter groups related routes. We include this in main.py with:
#   app.include_router(health_router, tags=["health"])
# The tags=["health"] argument makes this group visible in the auto-docs at /docs.
router = APIRouter()


@router.get(
    "/health",
    # response_model=None because we return different shapes on 200 vs 503.
    # FastAPI can't validate a response that changes shape, so we handle
    # serialisation ourselves via JSONResponse.
    summary="Check service health",
    description=(
        "Returns 200 if Postgres and Redis are reachable, "
        "503 with details if either is down."
    ),
)
async def health_check(
    # FastAPI resolves these arguments by calling the dependency functions
    # defined in app/dependencies.py. This is dependency injection:
    # the route function doesn't know HOW to create a DB session — it just
    # receives one. This makes the route trivially testable (swap in a mock).
    db: AsyncSession = Depends(get_db),
    redis_client: aioredis.Redis = Depends(get_redis),
) -> JSONResponse:
    """
    Ping Postgres and Redis. Return 200 if both are healthy, 503 otherwise.
    Each check result is surfaced individually so operators can see at a glance
    which dependency is failing without having to read logs.
    """
    checks: dict[str, str] = {}
    start = time.monotonic()  # monotonic clock for measuring duration reliably

    # --- Postgres health check ---
    # We run `SELECT 1` — the simplest possible query that proves:
    #   1. The network connection to Postgres is open
    #   2. The Postgres process is running and accepting queries
    #   3. Our credentials are valid
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        # We record the error message so the caller knows WHY it failed,
        # but we don't let the raw exception propagate (that would return 500).
        checks["database"] = f"error: {exc}"

    # --- Redis health check ---
    # redis-py's .ping() sends the PING command. Redis replies with PONG.
    # Same principle as SELECT 1 — proves connection + process are alive.
    try:
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    # Determine overall status: healthy only if ALL checks passed.
    all_ok = all(v == "ok" for v in checks.values())
    status = "healthy" if all_ok else "degraded"

    # HTTP status reflects the overall status:
    #   200 → everything fine, safe to route traffic here
    #   503 → something broken, stop routing traffic here
    http_status = 200 if all_ok else 503

    elapsed_ms = round((time.monotonic() - start) * 1000, 2)

    return JSONResponse(
        status_code=http_status,
        content={
            "status": status,
            "environment": settings.environment,
            "version": settings.version,
            # Individual check results let you pinpoint which dep is failing
            "checks": checks,
            # Latency of the health check itself — useful to spot a slow DB
            "response_time_ms": elapsed_ms,
        },
    )
