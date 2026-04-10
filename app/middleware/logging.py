"""
app/middleware/logging.py — request ID and timing middleware
=============================================================
This middleware does three things for every incoming request:

  1. Generates a unique request_id (UUID4) and binds it to the async context
     so EVERY log line emitted anywhere during this request automatically
     includes request_id — without anyone passing it explicitly.

  2. Logs one structured line when the request ARRIVES:
       {"event": "request started", "method": "POST", "path": "/chat", "client_ip": "1.2.3.4"}

  3. Logs one structured line when the response is SENT:
       {"event": "request finished", "status_code": 200, "duration_ms": 43.2}

Why is this in middleware and not in each route?
  If logging lived inside each route, you'd need to copy-paste the same
  boilerplate into 10+ routes. Worse, you'd forget it in some of them.
  Middleware runs for EVERY request automatically — routes stay clean.

What is request_id used for in production?
  When a user reports "something went wrong at 3pm", you find the request
  in your log aggregator by timestamp, grab its request_id, and query:
      request_id = "b8f3a21c-..."
  You instantly see every log line from that specific request across every
  module, service, and dependency — in chronological order.

How does request_id appear in every log line without being passed around?
  Python's `contextvars` module provides context-local storage for async
  code — similar to thread-local storage but safe for asyncio. We bind
  the request_id to the current async context with:
      structlog.contextvars.bind_contextvars(request_id=request_id)
  structlog's `merge_contextvars` processor (configured in logging_config.py)
  then automatically merges that value into every log event emitted in
  this async context — even from deep inside services and dependencies.

  The context is isolated per request: two simultaneous requests each
  get their own context with their own request_id.
"""

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Get a module-level logger.
# structlog.get_logger() is cheap — the actual configuration (JSON vs console)
# is resolved lazily on the first log call using the configuration set in
# logging_config.py. All logs from this module appear with
# {"logger": "app.middleware.logging"} in the output.
logger = structlog.get_logger(__name__)

# Paths that are excluded from request logging.
# The /health endpoint is hit every 5 minutes by UptimeRobot and every 30s
# by Railway's health check — logging every ping would generate thousands
# of noise log lines per day that obscure real application events.
_EXCLUDED_PATHS = {"/health", "/metrics"}


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware that adds a unique request_id to every request and logs
    structured request/response events.

    BaseHTTPMiddleware is Starlette's (and therefore FastAPI's) base class
    for writing middleware. You override `dispatch`, which receives the
    request and a `call_next` function. Calling `call_next(request)` passes
    the request down the middleware stack to the next middleware or the
    route handler, and returns the response.

    Execution flow:
        # Code here runs BEFORE the route handler
        response = await call_next(request)   # route handler runs here
        # Code here runs AFTER the route handler
        return response
    """

    async def dispatch(self, request: Request, call_next: object) -> Response:
        # Skip logging for health/metrics endpoints to reduce log noise.
        if request.url.path in _EXCLUDED_PATHS:
            return await call_next(request)  # type: ignore[operator]

        # --- Generate request_id ---
        # UUID4 is randomly generated — statistically impossible to collide.
        # We use the full UUID string (not a short hash) to guarantee uniqueness
        # even at high traffic volumes.
        request_id = str(uuid.uuid4())

        # --- Bind to async context ---
        # From this point forward, every log line emitted anywhere in the
        # async call stack for THIS request will automatically include
        # {"request_id": "b8f3a21c-..."} — services, dependencies, everything.
        #
        # clear_contextvars() first ensures we start with a clean slate —
        # if a previous request's context leaked (it shouldn't, but defensive
        # coding), we don't inherit its values.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            # Also bind environment so it appears in every log line.
            # Useful when multiple environments write to the same log aggregator.
        )

        # --- Also expose request_id in the response header ---
        # This lets clients (or support staff) include the request_id in a
        # bug report. You can then search your logs for that exact ID.
        # Convention: X- prefix for custom headers.

        # --- Log request arrival ---
        start_time = time.monotonic()  # monotonic clock: not affected by system time changes
        await logger.ainfo(
            "request started",
            method=request.method,
            path=request.url.path,
            # Client IP: check X-Forwarded-For first because Railway (and most
            # load balancers) strip the real IP and put it in this header.
            # Falling back to request.client.host gives the load balancer IP,
            # which is useless for rate limiting or debugging.
            client_ip=(
                request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (request.client.host if request.client else "unknown")
            ),
            user_agent=request.headers.get("user-agent", ""),
        )

        # --- Call the next middleware / route handler ---
        response: Response = await call_next(request)  # type: ignore[operator]

        # --- Log request completion ---
        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        await logger.ainfo(
            "request finished",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )

        # Attach the request_id to the response so it's visible in browser
        # DevTools and can be captured by the caller for support requests.
        response.headers["X-Request-ID"] = request_id

        return response
