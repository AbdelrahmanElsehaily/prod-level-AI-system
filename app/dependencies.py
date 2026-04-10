"""
app/dependencies.py — shared FastAPI dependency functions
=========================================================
FastAPI's dependency injection system works like this:
  1. You define a function (a "dependency") that produces some resource
     (a DB session, a Redis client, the current user, etc.)
  2. You declare it as a parameter with `Depends(your_function)` in a route
  3. FastAPI calls the function for every request and passes the result in

Why this pattern matters:
  - The route function doesn't know how to CREATE a DB session — it just
    uses one. This separation makes routes easy to unit test.
  - In tests, we replace these functions with mock versions using
    `app.dependency_overrides[get_db] = mock_get_db`. One line changes
    the behaviour for ALL routes in the test.
  - If you later change from asyncpg to a different driver, you only
    change this file — every route that uses `Depends(get_db)` gets the
    new implementation automatically.

Resources that should be shared across requests (like the Redis connection
pool) are created once at startup (in the lifespan in main.py) and stored
on `app.state`, then accessed here.
"""

from collections.abc import AsyncGenerator
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# We create ONE engine for the entire process lifetime.
# The engine manages a connection pool — it doesn't open a new TCP connection
# for every request; instead it keeps a pool of open connections and lends
# them out as needed. This is far more efficient.
#
# `echo=False` in production — setting it to True logs every SQL statement,
# which is useful for debugging but too noisy in production.
engine = create_async_engine(
    settings.database_url,
    echo=settings.environment == "development",  # SQL logging in dev only
    pool_size=5,        # Max connections kept open at all times
    max_overflow=10,    # Extra connections allowed when pool is exhausted
)

# async_sessionmaker is a factory that creates new AsyncSession objects.
# `expire_on_commit=False` means loaded objects stay accessible after commit,
# which is important in async code where you might access attributes after
# the session has closed.
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yield a database session for the duration of one request.

    Using `async with` (a context manager) ensures:
      - The session is always closed after the request, even if an exception
        occurs — no connection leaks.
      - If an exception happens mid-request, the transaction is rolled back
        automatically so you don't end up with partial writes.

    The `yield` makes this a generator function. FastAPI runs the code before
    yield at the start of the request, and the code after yield (the cleanup)
    at the end — even if an exception was raised.
    """
    async with AsyncSessionLocal() as session:
        yield session  # The route receives this session via Depends(get_db)
        # After yield: session.close() is called automatically by the context manager


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
async def get_redis(request: Request) -> aioredis.Redis:
    """
    Return the shared Redis client stored on app.state.

    Why store it on app.state?
      The Redis client maintains a connection pool internally. We want ONE
      pool shared across all requests — not a new connection per request.
      app.state is FastAPI's built-in place to store process-wide state.
      The client is created in main.py's lifespan startup and torn down on shutdown.

    Why inject via Depends rather than importing directly?
      In tests, we override this dependency to return a mock Redis client.
      If routes imported a Redis client directly (module-level), we couldn't
      swap it out without patching the module, which is fragile.
    """
    return request.app.state.redis  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Type aliases (optional convenience)
# ---------------------------------------------------------------------------
# These let you annotate route parameters concisely:
#   async def my_route(db: DB, redis: Redis) -> ...:
# instead of repeating the full `Annotated[..., Depends(...)]` each time.
DB = Annotated[AsyncSession, Depends(get_db)]
Redis = Annotated[aioredis.Redis, Depends(get_redis)]
