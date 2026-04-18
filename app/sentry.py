"""
app/sentry.py — Sentry error tracking initialisation
=====================================================
Sentry captures unhandled exceptions and sends them to the Sentry dashboard
with the full stack trace, request context, and breadcrumbs — so you find
out about production errors before users report them.

Why a separate module and not inline in main.py?
  Sentry initialisation has enough configuration (integrations, scrubbing,
  sampling) that it would clutter main.py. Isolating it here makes it easy
  to find, test, and change without touching the app entry point.

How Sentry works
  sentry_sdk.init() installs a global exception handler. Whenever an
  unhandled exception propagates out of a route (or any other code),
  Sentry intercepts it and sends an "event" to your project dashboard at
  sentry.io. The event includes:
    - The full exception traceback
    - The HTTP request that triggered it (method, URL, headers, body)
    - Breadcrumbs: a trail of log messages and DB queries leading up to the crash
    - Environment, release version, and tags you configure here

Integrations
  sentry_sdk supports automatic instrumentation via integrations. We enable:
    - FastApiIntegration: captures route exceptions, adds request context,
      and creates a transaction per request for performance monitoring
    - SqlalchemyIntegration: traces every DB query as a "span" so you can
      see which queries are slow or which ones happen before a crash

Optional initialisation
  Sentry is only initialised when SENTRY_DSN is set. This means:
    - Local development: leave SENTRY_DSN unset → Sentry is completely disabled,
      no network calls, no performance overhead
    - Production: set SENTRY_DSN → full error tracking enabled automatically
    - Unit tests: ENVIRONMENT=test and no DSN → Sentry never initialises

PII scrubbing
  By default Sentry captures request bodies and headers. Some of those may
  contain secrets (Authorization headers, API keys) or personal data. We
  configure two layers of protection:
    1. send_default_pii=False — disables automatic capture of cookies, session
       data, and user IP addresses
    2. before_send hook — strips specific sensitive headers and fields from
       every event before it leaves the process. Even if Sentry's defaults
       change, our hook always runs.
"""

from collections.abc import Mapping
from typing import Any

import sentry_sdk
import structlog
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.types import Event

from app.config import settings

logger = structlog.get_logger(__name__)

# Headers whose values must never appear in Sentry events.
# These are checked case-insensitively against the request headers dict.
_SENSITIVE_HEADERS = {
    "authorization",  # Bearer tokens, API keys passed in Authorization header
    "x-api-key",  # common alternative API key header
    "cookie",  # session cookies may contain auth tokens
}

# Top-level event keys and nested field names to redact.
# "password" and "token" cover most auth-related fields in request bodies.
_SENSITIVE_FIELD_NAMES = {"password", "token", "api_key", "secret"}


def _scrub_sensitive_data(
    event: Event,
    hint: dict[str, Any],  # noqa: ARG001 — required by Sentry's before_send signature
) -> Event | None:
    """
    Sentry before_send hook — strips sensitive data before the event is sent.

    This function is called for every event (exception, message) before Sentry
    transmits it. Returning None drops the event entirely. Returning the (possibly
    modified) event sends it.

    Why before_send instead of relying on Sentry's built-in scrubbing?
      Sentry has a "Data Scrubbing" feature in the dashboard, but it runs server-
      side AFTER the data has already left your process and crossed the network.
      before_send runs CLIENT-SIDE before any data is transmitted — stronger
      guarantee that sensitive values never leave your infrastructure.

    Args:
        event: The Sentry event dict. We mutate it in place and return it.
        hint:  Additional context (the original exception object, etc.).
               Not used here but required by Sentry's callback signature.

    Returns:
        The scrubbed event dict, or None to drop the event entirely.
    """
    # --- Scrub request headers ---
    # Sentry captures the full HTTP request including headers. Authorization
    # headers contain Bearer tokens that must not appear in the dashboard.
    request = event.get("request", {})
    if isinstance(request, Mapping):
        headers = request.get("headers", {})
        if isinstance(headers, dict):
            for header_name in list(headers.keys()):
                if header_name.lower() in _SENSITIVE_HEADERS:
                    headers[header_name] = "[Filtered]"

    # --- Scrub request body fields ---
    # POST /chat sends {"message": "...", "conversation_id": "..."} — safe.
    # But future endpoints might accept passwords or tokens in the body.
    # This loop replaces any field whose name matches our sensitive list.
    data = request.get("data", {}) if isinstance(request, Mapping) else {}
    if isinstance(data, dict):
        for field_name in list(data.keys()):
            if field_name.lower() in _SENSITIVE_FIELD_NAMES:
                data[field_name] = "[Filtered]"

    # --- Scrub extra / tags ---
    # Developers sometimes accidentally log secrets via sentry_sdk.set_extra()
    # or capture_message(). Scrub those too.
    extra = event.get("extra", {})
    if isinstance(extra, dict):
        for key in list(extra.keys()):
            if key.lower() in _SENSITIVE_FIELD_NAMES:
                extra[key] = "[Filtered]"

    return event


def init_sentry() -> None:
    """
    Initialise the Sentry SDK if SENTRY_DSN is configured.

    Call this once at application startup (in main.py's lifespan), after
    logging is configured so any Sentry initialisation warnings appear in
    structured logs.

    Safe to call multiple times — if SENTRY_DSN is not set, this is a no-op.
    """
    if not settings.sentry_dsn:
        # Log at debug level so local dev isn't spammed, but the absence
        # is visible if someone is debugging why Sentry isn't receiving events.
        logger.debug("sentry dsn not set — error tracking disabled")
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        # environment tags every Sentry event so you can filter by
        # "production" vs "staging" in the dashboard.
        environment=settings.environment,
        # release ties events to a specific code version. Using the app
        # version from settings means you can see which deploy introduced a bug.
        release=f"chat-api@{settings.version}",
        # traces_sample_rate: fraction of requests to trace for performance.
        # 1.0 = 100% (too expensive in production), 0.0 = none.
        # 0.1 = 10% is a good starting point — enough data to spot slow endpoints
        # without significant overhead or Sentry quota usage.
        traces_sample_rate=0.1,
        # send_default_pii=False: do NOT automatically attach cookies, session
        # data, or user IP addresses to events. We only capture what we
        # explicitly add via set_user() or set_tag().
        send_default_pii=False,
        integrations=[
            # FastApiIntegration: auto-captures route exceptions and creates
            # one Sentry "transaction" per HTTP request for performance tracing.
            # transaction_style="endpoint" names transactions by route pattern
            # (e.g. "POST /chat") rather than URL path (e.g. "POST /chat").
            # Route-based naming groups all requests to /chat together in the
            # performance dashboard regardless of query params.
            FastApiIntegration(transaction_style="endpoint"),
            # SqlalchemyIntegration: traces every SQLAlchemy query as a "span"
            # inside the request transaction. You can see exactly which queries
            # ran, how long they took, and which one was slow before a crash.
            SqlalchemyIntegration(),
        ],
        # before_send: our custom hook to strip sensitive data before
        # any event is transmitted over the network to Sentry's servers.
        before_send=_scrub_sensitive_data,
    )

    logger.info(
        "sentry initialised",
        environment=settings.environment,
        release=f"chat-api@{settings.version}",
        traces_sample_rate=0.1,
    )
