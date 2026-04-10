"""
app/middleware/
===============
ASGI middleware wraps every request/response cycle. Think of it as a
pipeline that every HTTP request passes through before reaching a route,
and every HTTP response passes through on the way back out.

  Incoming request:
    client → [rate_limit middleware] → [logging middleware] → route handler

  Outgoing response:
    route handler → [logging middleware] → [rate_limit middleware] → client

This makes middleware the right place for cross-cutting concerns — things
every request needs — without duplicating that logic in every route:

  logging.py    — generates a unique request_id, logs start/end of request  (Step 3)
  rate_limit.py — per-IP rate limiting via Redis sliding window              (Step 6)

Middleware is registered in app/main.py via:
    app.add_middleware(LoggingMiddleware)

The ORDER middleware is added matters: the last one added runs FIRST on
incoming requests (like a stack). We add rate limiting last so it runs
before logging — a rate-limited request still gets its own request_id log.
"""
