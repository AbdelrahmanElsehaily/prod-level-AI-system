"""
app/middleware/rate_limit.py — per-IP sliding window rate limiting
==================================================================
Limits each IP address to a configurable number of requests per minute.
Clients that exceed the limit receive HTTP 429 Too Many Requests.

Why rate limiting?
  Without it, a single misbehaving client (a buggy frontend, a scraper, or
  a deliberate attack) can:
    - Exhaust your AI API quota in minutes
    - Overload Postgres with concurrent queries
    - Make the service slow or unavailable for everyone else

  Rate limiting is your first line of defence. It doesn't stop a determined
  attacker, but it stops accidents and careless clients immediately.

Why Redis for rate limiting?
  Rate limiting state must be shared across all processes. If you store
  "this IP has made 18 requests" in Python memory and you have 4 uvicorn
  workers, each worker has an independent counter — the limit effectively
  becomes 4x what you intended.

  Redis is an in-memory data store that all processes share. One key in
  Redis = one counter, regardless of how many app processes are running.

Sliding window vs fixed window
  Fixed window: "you get 20 requests per minute" — the window resets every
  60 seconds on the clock. Problem: a client can send 20 requests at 0:59
  and 20 more at 1:01 — 40 requests in 2 seconds, double the intended limit.

  Sliding window: the window moves with time. At any moment, we count
  requests in the past 60 seconds. A client can never exceed the limit
  in any rolling 60-second period, regardless of where the clock is.

  We implement sliding window using a Redis sorted set:
    - Each request is stored as a member with its timestamp as the score
    - "Count in the last 60 seconds" = ZCOUNT key (now-60s) now
    - "Remove old requests" = ZREMRANGEBYSCORE key 0 (now-60s)

Algorithm (per request):
  1. Compute window_start = now - 60 seconds
  2. Remove all members older than window_start (they're outside the window)
  3. Count the remaining members (= requests in the last 60 seconds)
  4. If count >= limit: return 429, don't process the request
  5. Add this request to the sorted set with score = now
  6. Set TTL on the key = 60 seconds (auto-cleanup if the client goes quiet)
  7. Process the request normally, add rate limit headers to the response
"""

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
# How many requests one IP is allowed in one window.
RATE_LIMIT = 20

# Length of the sliding window in seconds.
WINDOW_SECONDS = 60

# Paths completely exempt from rate limiting.
# The /health endpoint is hit every few minutes by monitoring tools — we must
# never block it, or our uptime monitor will report false outages.
_EXCLUDED_PATHS = {"/health", "/metrics"}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware that enforces per-IP sliding window rate limits via Redis.

    Added to the app in main.py with:
        app.add_middleware(RateLimitMiddleware)

    Middleware executes around every request. This one:
      - Skips excluded paths immediately (health checks, metrics)
      - Looks up the client's IP in Redis to count recent requests
      - Rejects with 429 if over the limit
      - Adds rate limit headers to every allowed response
    """

    async def dispatch(self, request: Request, call_next: object) -> Response:
        # Fast path: never rate-limit excluded endpoints.
        if request.url.path in _EXCLUDED_PATHS:
            return await call_next(request)  # type: ignore[operator]

        # --- Identify the client ---
        # X-Forwarded-For is set by Railway's load balancer and contains the
        # real client IP. Without this, every request appears to come from the
        # load balancer's IP and everyone shares one rate limit counter.
        client_ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )

        # --- Get the Redis client ---
        # Stored on app.state by the lifespan in main.py.
        # If Redis is unavailable, we fail open (allow the request) rather
        # than fail closed (block everyone). A rate limiter being down is less
        # bad than the entire API being down.
        redis = getattr(request.app.state, "redis", None)
        if redis is None:
            return await call_next(request)  # type: ignore[operator]

        # --- Sliding window check ---
        redis_key = f"rate_limit:{client_ip}"
        now = time.time()
        window_start = now - WINDOW_SECONDS

        try:
            # Use a Redis pipeline (transaction) to run all commands atomically.
            # Without a pipeline, another request could sneak in between our
            # ZREMRANGEBYSCORE and ZCARD, causing an inaccurate count.
            #
            # pipeline(transaction=True) wraps the commands in MULTI/EXEC —
            # Redis executes them as a single atomic unit.
            pipe = redis.pipeline(transaction=True)

            # Step 1: Remove requests older than the window start.
            # ZREMRANGEBYSCORE removes all members with score (timestamp) in
            # the range [0, window_start]. Those are outside our 60s window.
            pipe.zremrangebyscore(redis_key, 0, window_start)

            # Step 2: Count how many requests remain in the window.
            pipe.zcard(redis_key)

            # Step 3: Add the current request to the sorted set.
            # Score = current timestamp (float seconds).
            # Member = unique ID so two simultaneous requests don't collide
            # (sorted sets deduplicate members with the same name).
            pipe.zadd(redis_key, {str(uuid.uuid4()): now})

            # Step 4: Set the key to expire after one window.
            # Without this, keys for IPs that never return stay in Redis forever.
            # TTL = WINDOW_SECONDS + 1 second buffer to avoid a race where the
            # key expires just before the last request's window closes.
            pipe.expire(redis_key, WINDOW_SECONDS + 1)

            # Execute all four commands atomically and collect results.
            # results[0] = number removed (zremrangebyscore)
            # results[1] = count before adding current request (zcard)
            # results[2] = 1 if added, 0 if already existed (zadd)
            # results[3] = 1 if TTL was set (expire)
            results = await pipe.execute()
            current_count = results[1]  # count BEFORE this request was added

        except Exception as exc:
            # Redis error — log it and allow the request (fail open).
            # We'd rather serve traffic than block everyone because Redis hiccupped.
            await logger.awarning(
                "rate limit redis error — failing open",
                client_ip=client_ip,
                error=str(exc),
            )
            return await call_next(request)  # type: ignore[operator]

        # --- Compute header values ---
        remaining = max(0, RATE_LIMIT - current_count - 1)
        # Reset time = start of the current window + window length
        # i.e. the earliest time when the oldest request in the window falls off.
        reset_at = int(window_start + WINDOW_SECONDS + 1)

        # --- Enforce the limit ---
        if current_count >= RATE_LIMIT:
            # Remove the request we just added — it was rejected, don't count it.
            # This keeps the sorted set clean so a burst of rejected requests
            # doesn't falsely inflate the count after the window slides.
            try:
                await redis.zremrangebyscore(redis_key, now, now)
            except Exception:
                pass  # best-effort cleanup, don't fail the rejection response

            await logger.awarning(
                "rate limit exceeded",
                client_ip=client_ip,
                count=current_count,
                limit=RATE_LIMIT,
            )

            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    # retry_after tells the client how long to wait before
                    # trying again. WINDOW_SECONDS is a conservative upper bound —
                    # in practice the oldest request may fall off the window sooner.
                    "retry_after_seconds": WINDOW_SECONDS,
                },
                headers={
                    "X-RateLimit-Limit": str(RATE_LIMIT),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_at),
                    # Retry-After is a standard HTTP header (RFC 7231).
                    # Well-behaved clients (and API gateways) respect it.
                    "Retry-After": str(WINDOW_SECONDS),
                },
            )

        # --- Request is within the limit — process it ---
        response: Response = await call_next(request)  # type: ignore[operator]

        # Add rate limit headers to every successful response so clients can
        # track their usage without waiting for a 429.
        response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_at)

        return response
