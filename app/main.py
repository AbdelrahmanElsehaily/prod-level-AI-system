"""
app/main.py — FastAPI application entry point
==============================================
This file creates the FastAPI app instance and wires everything together:
  - Lifespan: startup (logging, migrations, Redis pool) and shutdown (cleanup)
  - Middleware: registered in reverse execution order (last added = first to run)
  - Routers: each domain's endpoints mounted here

No business logic lives here — that belongs in routers/ and services/.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as aioredis
from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from app.config import settings
from app.logging_config import setup_logging
from app.middleware.logging import LoggingMiddleware
from app.routers import health

import structlog

logger = structlog.get_logger(__name__)


def run_migrations() -> None:
    """
    Apply all pending Alembic migrations synchronously at startup.

    Why run migrations at startup?
      In production every new deployment may include schema changes. Running
      migrations here (before the app starts serving traffic) means:
        - The schema is always in sync with the code that runs against it
        - If a migration fails, the app never starts → deployment platform
          rolls back → no traffic served against the wrong schema
        - No manual `alembic upgrade head` step needed in the deploy pipeline

    Why synchronous?
      Alembic is synchronous by design (it uses psycopg2 / sync connections
      internally via our env.py setup). We call it here before the async event
      loop handles any requests — there's no concurrency conflict.

    In production (Railway), the railway.toml start command runs:
        alembic upgrade head && uvicorn app.main:app ...
    meaning migrations run BEFORE the process even starts. This function is
    the local-dev / Docker equivalent so `docker compose up` also works.
    """
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

    # 2. Run database migrations.
    #    This is synchronous and blocks briefly at startup — acceptable because
    #    it runs once, before the event loop accepts any requests.
    await logger.ainfo("running database migrations")
    run_migrations()
    await logger.ainfo("database migrations complete")

    # 3. Create the shared Redis connection pool.
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
# Current order (first to run → last):
#   1. LoggingMiddleware — attaches request_id, logs request start/end
# Step 6 adds RateLimitMiddleware before LoggingMiddleware.
app.add_middleware(LoggingMiddleware)

# --- Routers ---
app.include_router(health.router, tags=["Health"])


@app.get("/", tags=["Root"])
async def root() -> dict[str, str]:
    """Minimal root endpoint — confirms the app is reachable."""
    return {"status": "ok"}
