"""
app/main.py — FastAPI application entry point
==============================================
This file creates the FastAPI app instance and wires everything together:
  - Lifespan: startup (logging, Redis pool) and shutdown (cleanup)
  - Middleware: registered in reverse execution order (last added = first to run)
  - Routers: each domain's endpoints mounted here

No business logic lives here — that belongs in routers/ and services/.

Migrations
----------
Database migrations are NOT run here. They are run by the Railway startCommand
BEFORE uvicorn starts:

  alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT

Running migrations in the lifespan as well would be redundant and fragile:
  - Redundant because startCommand already guarantees the schema is up to date
    before the first request is ever served.
  - Fragile because calling asyncio.run() from a run_in_executor worker thread
    creates a second event loop, which can cause subtle bugs under load.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI

from app.config import settings
from app.langfuse_client import flush as flush_langfuse
from app.logging_config import setup_logging
from app.middleware.logging import LoggingMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.routers import chat, debug, documents, health, metrics, rag
from app.sentry import init_sentry

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manage process-wide resources that live for the full lifetime of the app.

    Everything BEFORE yield → startup (runs once when the process starts)
    Everything AFTER yield  → shutdown (runs even if the app crashes)

    Startup order:
      1. Logging  — so any startup errors are captured in structured JSON logs
      2. Sentry   — so startup crashes are tracked before they kill the process
      3. Redis    — open the connection pool so requests can use it immediately

    Note: migrations are NOT run here. They are handled by the Railway
    startCommand before uvicorn starts:
      alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT
    """
    # 1. Configure structured logging before anything else logs.
    setup_logging()

    # 2. Initialise Sentry error tracking.
    #    Called immediately after logging so any startup errors are captured
    #    by Sentry before they crash the process.
    #    No-op if SENTRY_DSN is not set (local dev, unit tests).
    init_sentry()

    # 3. Create the shared Redis connection pool.
    #    Stored on app.state so all requests share one pool (not one connection
    #    per request). decode_responses=True → Redis returns str, not bytes.
    app.state.redis = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
    )

    yield  # ← The application runs here, handling requests

    # Shutdown: flush Langfuse's background queue first — this drains any
    # pending trace events so they reach the Langfuse API before the process
    # exits. If we closed Redis first, Langfuse flushes fine (it uses its own
    # HTTP connection, not Redis), so order here doesn't matter much.
    flush_langfuse()

    # Shutdown: close the Redis pool gracefully so Redis doesn't log
    # "connection closed unexpectedly" warnings.
    await app.state.redis.aclose()


app = FastAPI(
    title="Chat API",
    version=settings.version,
    description="Production-level chat API backed by Anthropic Claude",
    lifespan=lifespan,
)

# --- Middleware ---
# add_middleware() calls form a STACK: last added = first to run on requests.
#
# Starlette processes middleware in REVERSE registration order:
#   last added → runs FIRST (outermost layer)
#   first added → runs LAST (innermost layer, closest to the route)
#
# We want:
#   RateLimitMiddleware (outermost) → reject over-limit requests before logging
#   LoggingMiddleware (innermost)   → log only requests that pass rate limit
#
# To achieve this order we register LoggingMiddleware FIRST, then RateLimitMiddleware.
# The last-registered middleware wraps all the others.
app.add_middleware(LoggingMiddleware)
app.add_middleware(RateLimitMiddleware)

# --- Routers ---
app.include_router(health.router, tags=["Health"])
app.include_router(metrics.router, tags=["Metrics"])
app.include_router(chat.router, tags=["Chat"])
app.include_router(documents.router)  # prefix + tags set on the router itself
app.include_router(rag.router)  # mounts POST /chat/docs — see app/routers/rag.py

# Debug router: only mounted in non-production environments.
#
# Why conditional mounting instead of a runtime check inside the route?
#   A runtime check (if settings.environment == "production": raise 404)
#   still registers the route in FastAPI's routing table — it appears in
#   /docs and is technically reachable. Conditional mounting means the route
#   does not exist at all in production: no /docs entry, no routing overhead,
#   and no risk of the guard being accidentally removed.
if settings.environment != "production":
    app.include_router(debug.router)


@app.get("/", tags=["Root"])
async def root() -> dict[str, str]:
    """Minimal root endpoint — confirms the app is reachable."""
    return {"status": "ok"}
