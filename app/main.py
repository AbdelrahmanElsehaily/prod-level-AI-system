"""
app/main.py — FastAPI application entry point
==============================================
This file has one job: create the FastAPI app instance and wire everything
together. It should contain NO business logic — that lives in routers/ and
services/.

Key concepts introduced here:

1. Lifespan context manager
   FastAPI replaced @app.on_event("startup") / @app.on_event("shutdown")
   with a single lifespan context manager (Python 3.10+). Everything before
   `yield` runs at startup; everything after runs at shutdown.
   This keeps startup and shutdown paired in one place, making it impossible
   to forget to clean up a resource you opened.

2. app.state
   FastAPI's built-in place to store process-wide objects that should be
   shared across requests but NOT re-created on every request.
   We store the Redis client here so every request uses the same connection pool.

3. include_router
   Mounts a group of routes from a router file into the main app.
   This is how we keep routes organised as the API grows — each domain
   (health, chat, metrics) lives in its own file.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI

from app.config import settings
from app.routers import health


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manage resources that must live for the full lifetime of the process.

    Startup (code before yield):
      - Create the Redis connection pool. We do this once so all requests
        share the same pool rather than opening a new TCP connection each time.

    Shutdown (code after yield):
      - Close the Redis connection pool gracefully. This flushes any buffered
        commands and closes TCP connections cleanly rather than dropping them.
        Without this, Redis may log "connection closed unexpectedly" errors.

    FastAPI guarantees the cleanup runs even if the app crashes —
    similar to a try/finally block.
    """
    # --- STARTUP ---
    app.state.redis = aioredis.from_url(
        settings.redis_url,
        # decode_responses=True means Redis returns Python strings instead of
        # raw bytes. Without this, every value you read is b"some bytes" and
        # you have to decode it manually everywhere.
        decode_responses=True,
    )

    yield  # Application runs here — handling requests

    # --- SHUTDOWN ---
    await app.state.redis.aclose()


# Create the FastAPI application instance.
# - title / version appear in the auto-generated docs at /docs
# - lifespan=lifespan wires up our startup/shutdown logic
app = FastAPI(
    title="Chat API",
    version=settings.version,
    description="Production-level chat API backed by Anthropic Claude",
    lifespan=lifespan,
)

# --- Mount routers ---
# include_router registers all routes defined in health.router with the app.
# tags=["Health"] groups these endpoints under a "Health" section in /docs.
app.include_router(health.router, tags=["Health"])


# Keep the root route from Step 1.
# In a real production app this might redirect to /docs or return API metadata.
@app.get("/", tags=["Root"])
async def root() -> dict[str, str]:
    """Minimal root endpoint — confirms the app is reachable."""
    return {"status": "ok"}
