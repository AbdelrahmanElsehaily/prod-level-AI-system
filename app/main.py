"""
app/main.py — FastAPI application entry point
==============================================
This file creates the FastAPI app instance and wires everything together:
  - Lifespan: startup (logging, migrations, Redis pool) and shutdown (cleanup)
  - Middleware: registered in reverse execution order (last added = first to run)
  - Routers: each domain's endpoints mounted here

No business logic lives here — that belongs in routers/ and services/.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import structlog
from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from app.config import settings
from app.logging_config import setup_logging
from app.middleware.logging import LoggingMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.routers import chat, debug, health
from app.sentry import init_sentry

logger = structlog.get_logger(__name__)


def run_migrations() -> None:
    """
    Apply all pending Alembic migrations.

    This function is intentionally synchronous. It is called from the async
    lifespan via asyncio.get_event_loop().run_in_executor(), which runs it
    in a worker thread. That thread has no running event loop, which lets
    migrations/env.py call asyncio.run() safely to drive the async Alembic
    runner (see the lifespan comment for the full explanation).

    Why run migrations at startup?
      In production every new deployment may include schema changes. Running
      migrations here (before the app starts serving traffic) means:
        - The schema is always in sync with the code that runs against it
        - If a migration fails, the app never starts → deployment platform
          rolls back → no traffic served against the wrong schema
        - No manual `alembic upgrade head` step needed in the deploy pipeline

    Why skip in test environment?
      Unit tests mock the database entirely (via dependency_overrides), so there
      is no real Postgres to migrate against. Skipping is correct behaviour,
      not a workaround — we are not testing migrations here.

      Integration tests (tests/integration/) run against a real Postgres and
      DO need migrations applied. They handle that by setting ENVIRONMENT to
      anything other than "test", or by calling alembic upgrade head directly
      in the CI service container setup step.
    """
    if settings.environment == "test":
        # No real DB in unit tests — all DB calls are mocked via
        # dependency_overrides. Running migrations would fail anyway
        # (no Postgres listening), and would hit the asyncio.run()
        # conflict described above.
        return

    alembic_cfg = Config("alembic.ini")
    # Override the database URL from settings rather than alembic.ini,
    # so the same config file works in every environment without editing it.
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(alembic_cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manage process-wide resources that live for the full lifetime of the app.

    Everything BEFORE yield → startup (runs once when the process starts)
    Everything AFTER yield  → shutdown (runs even if the app crashes)

    Startup order matters:
      1. Logging  — so any startup errors are captured in structured JSON logs
      2. Migrations — apply DB schema changes before serving any traffic
      3. Redis    — open the connection pool so requests can use it immediately
    """
    # 1. Configure structured logging before anything else logs.
    setup_logging()

    # 2. Initialise Sentry error tracking.
    #    Called immediately after logging so any startup errors (migrations,
    #    Redis connection) are captured by Sentry before they crash the process.
    #    No-op if SENTRY_DSN is not set (local dev, unit tests).
    init_sentry()

    # 3. Run database migrations in a thread-pool executor.
    #
    #    Why a thread executor instead of a plain call?
    #
    #    migrations/env.py ends with:
    #        asyncio.run(run_migrations_online())
    #
    #    asyncio.run() creates a BRAND NEW event loop. If called from a thread
    #    that already has a running loop (uvicorn's), Python raises:
    #        RuntimeError: asyncio.run() cannot be called from a running event loop
    #
    #    run_in_executor(None, ...) offloads the call to a worker thread from
    #    the default ThreadPoolExecutor. That thread has NO running event loop,
    #    so asyncio.run() inside env.py can safely create one there.
    #
    #    The await here suspends the lifespan coroutine until the worker thread
    #    finishes — migrations complete before any request is served.
    await logger.ainfo("running database migrations")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_migrations)
    await logger.ainfo("database migrations complete")

    # 4. Create the shared Redis connection pool.
    #    Stored on app.state so all requests share one pool (not one connection
    #    per request). decode_responses=True → Redis returns str, not bytes.
    app.state.redis = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
    )

    yield  # ← The application runs here, handling requests

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
app.include_router(chat.router, tags=["Chat"])

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
