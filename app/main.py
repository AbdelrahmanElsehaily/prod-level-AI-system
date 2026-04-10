"""
app/main.py — FastAPI application entry point
==============================================
This file creates the FastAPI app instance and wires everything together:
  - Lifespan: startup (open Redis pool, init logging) and shutdown (close pool)
  - Middleware: registered in reverse execution order (last added = first to run)
  - Routers: each domain's endpoints mounted here

No business logic lives here — that belongs in routers/ and services/.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI

from app.config import settings
from app.logging_config import setup_logging
from app.middleware.logging import LoggingMiddleware
from app.routers import health


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manage process-wide resources that live for the full lifetime of the app.

    Everything BEFORE yield → startup
    Everything AFTER yield  → shutdown (runs even if the app crashes)

    Startup order matters:
      1. Logging first — so any errors during startup are captured in structured logs.
      2. Redis second — so the connection pool is ready before requests arrive.
    """
    # 1. Configure structured logging before anything else logs.
    #    After this call, all log output is JSON (production) or
    #    colour-coded text (development) with consistent fields.
    setup_logging()

    # 2. Create the Redis connection pool and store it on app.state.
    #    app.state is FastAPI's built-in process-wide key-value store.
    #    Storing the client here means all requests share ONE pool instead
    #    of opening a new TCP connection per request.
    app.state.redis = aioredis.from_url(
        settings.redis_url,
        # decode_responses=True: Redis returns str instead of bytes.
        # Without this, every cached value would be b"..." and need manual decoding.
        decode_responses=True,
    )

    yield  # ← The application runs here, handling requests

    # Shutdown: close the Redis pool gracefully.
    # aclose() sends a QUIT command, waits for in-flight commands, and
    # closes the underlying TCP connections. Without this, the OS forcibly
    # closes the sockets and Redis logs "connection closed unexpectedly".
    await app.state.redis.aclose()


app = FastAPI(
    title="Chat API",
    version=settings.version,
    description="Production-level chat API backed by Anthropic Claude",
    lifespan=lifespan,
)

# --- Middleware ---
# Middleware wraps every request/response cycle.
# add_middleware() calls form a STACK — the last one added runs FIRST
# on incoming requests (and last on outgoing responses).
#
# Current stack (first to run on incoming requests → last):
#   1. LoggingMiddleware — generates request_id, logs start/end of every request
#
# Step 6 will prepend RateLimitMiddleware so it runs before logging.
app.add_middleware(LoggingMiddleware)

# --- Routers ---
# include_router mounts all routes from a router module onto the app.
# tags=["Health"] groups these endpoints under "Health" in the /docs UI.
app.include_router(health.router, tags=["Health"])


@app.get("/", tags=["Root"])
async def root() -> dict[str, str]:
    """Minimal root endpoint — confirms the app is reachable."""
    return {"status": "ok"}
