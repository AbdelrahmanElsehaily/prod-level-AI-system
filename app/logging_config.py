"""
app/logging_config.py — structlog configuration
================================================
This module configures how log output is formatted for the entire application.
Call `setup_logging()` once at startup (in main.py) before anything else logs.

Why structlog over Python's built-in logging?
----------------------------------------------
Python's `logging` produces plain strings:
    INFO 2024-01-01 12:00:00 - request started path=/chat

You cannot filter those by field in a log aggregator. You'd have to parse
the string with a regex — fragile and error-prone.

structlog produces dictionaries that serialise to JSON in production:
    {"level":"info","timestamp":"2024-01-01T12:00:00Z","event":"request started","path":"/chat","request_id":"abc-123"}

Now in Grafana Loki / Datadog / CloudWatch Logs you can query:
    request_id = "abc-123"         → every log line from one specific request
    level = "error"                → all errors across all requests
    path = "/chat" AND level = "warning"   → slow or failing chat requests

Two rendering modes
-------------------
We use different renderers depending on ENVIRONMENT:
  - development: ConsoleRenderer — colour-coded, human-readable, easy to read in a terminal
  - production:  JSONRenderer   — machine-readable, one JSON object per line, parsed by log aggregators

The ENVIRONMENT env var (read from config.py) controls which is used.
Switching is zero-config: deploy to Railway with ENVIRONMENT=production and
JSON logging is automatically enabled.

How structlog works
-------------------
Every log call passes through a chain of "processors" in order:
    log.info("request started", path="/chat")
      → add_log_level        adds {"level": "info"}
      → add_timestamp        adds {"timestamp": "2024-01-01T12:00:00Z"}
      → merge_contextvars    adds any fields bound to the current context
                             (e.g. request_id bound in the middleware)
      → [renderer]           serialises to JSON string or colour-coded text
      → [output]             writes to stdout

The key power: `merge_contextvars`. Any field bound with `structlog.contextvars.bind_contextvars(request_id="abc")`
is automatically included in EVERY subsequent log line in that async context —
without passing the request_id explicitly to every logger call.
"""

import logging
import sys

import structlog

from app.config import settings


def setup_logging() -> None:
    """
    Configure structlog processors and wire it into Python's standard logging.

    Call this once, at the top of main.py's lifespan startup, before any
    other code runs. Calling it multiple times is safe (processors are replaced).
    """

    # Shared processors run on EVERY log line regardless of environment.
    # Order matters — each processor receives the output of the previous one.
    shared_processors: list[structlog.types.Processor] = [
        # merge_contextvars: pull in any key-value pairs bound with
        # structlog.contextvars.bind_contextvars() in the current async context.
        # This is how request_id flows from the middleware into every log line
        # automatically — the middleware binds it once, and every log call
        # within that request picks it up from context.
        structlog.contextvars.merge_contextvars,

        # add_log_level: adds {"level": "info"} / {"level": "error"} etc.
        structlog.stdlib.add_log_level,

        # add_logger_name: adds {"logger": "app.routers.health"} so you can
        # tell which module emitted the log line.
        structlog.stdlib.add_logger_name,

        # TimeStamper: adds {"timestamp": "2024-01-01T12:00:00.123456Z"}
        # utc=True ensures all timestamps are in UTC regardless of server timezone.
        # ISO format is standard and parseable by every log aggregator.
        structlog.processors.TimeStamper(fmt="iso", utc=True),

        # StackInfoRenderer: if log.exception() is called, this formats the
        # exception traceback as a structured field instead of a raw string.
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.environment == "development":
        # Development: human-readable, colour-coded output.
        # ConsoleRenderer adds colours for level (green=info, red=error),
        # bold for the event message, and aligned columns.
        # Example output:
        #   2024-01-01T12:00:00Z [info     ] request started  path=/chat method=POST request_id=abc-123
        processors: list[structlog.types.Processor] = shared_processors + [
            # In dev, format exceptions with full tracebacks inline
            structlog.dev.ConsoleRenderer(colors=True),
        ]
    else:
        # Production: machine-readable JSON, one object per line.
        # Each line is a complete, valid JSON object that log aggregators
        # can parse, index, and query by field.
        # Example output:
        #   {"level":"info","logger":"app.middleware.logging","timestamp":"2024-01-01T12:00:00Z","event":"request started","path":"/chat","method":"POST","request_id":"abc-123"}
        processors = shared_processors + [
            # ExceptionRenderer: formats exceptions as a structured "exception"
            # key in the JSON object — not a multi-line string that breaks JSON parsing.
            structlog.processors.ExceptionRenderer(),

            # JSONRenderer: serialises the entire event dict to a JSON string.
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        # Use structlog's AsyncBoundLogger for async code. It defers
        # the actual log rendering to a thread so it doesn't block the
        # event loop on JSON serialisation.
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        # route structlog output through Python's standard logging module
        # so third-party libraries that use standard logging also appear
        # in our structured output.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Also configure Python's standard logging so libraries like uvicorn,
    # SQLAlchemy, and asyncpg respect our log level setting.
    logging.basicConfig(
        format="%(message)s",   # structlog handles formatting; stdlib just passes through
        stream=sys.stdout,
        level=logging.INFO,
    )

    # Silence overly chatty libraries that would pollute the log output.
    # These are set to WARNING so only real problems surface.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # replaced by our middleware
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
