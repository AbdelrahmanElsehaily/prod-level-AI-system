"""
app/langfuse_client.py — Langfuse observability client (singleton)
===================================================================
Langfuse is an open-source AI observability platform. It records every LLM call
as a "trace" — capturing the input, output, token counts, latency, and any
metadata you attach. This gives you a searchable history of everything your AI
has ever done, which is invaluable for debugging wrong answers, tracking cost,
and monitoring quality over time.

Why a separate module?
  The Langfuse client is initialised once and reused everywhere. If we created
  it inside ai.py, tests that import ai.py would attempt to initialise Langfuse
  even if the credentials aren't set. Centralising it here — with a clean
  no-op sentinel — makes the rest of the code simple:

    from app.langfuse_client import langfuse
    if langfuse:
        # do tracing

Why a function (init_langfuse) rather than inline module-level code?
  The same reason we have init_sentry() instead of inline sentry_sdk.init():

  1. TESTABILITY — tests can call init_langfuse() with patched settings
     and get back the result without reloading the whole module. Module-level
     code runs at import time, which is before tests can patch anything.

  2. CLARITY — readers can see the guard condition (credentials present?)
     and the construction in one place, rather than scattered across a
     try/except/if block at the top level.

  3. CONSISTENCY — mirrors the pattern already used for Sentry in
     app/sentry.py. Same shape = easier to understand the codebase.

Langfuse concepts (used in ai.py):
  Trace      — one end-to-end conversation. We key traces on conversation_id
               so every AI call in a conversation is grouped together.
  Generation — one LLM call within a trace. Records the model, input prompt,
               output text, token counts, latency, and estimated cost.
  Score      — optional quality rating attached to a trace (not used here yet).

Flush on shutdown:
  Langfuse batches events and sends them asynchronously. If the process exits
  before the queue drains, events are lost. The flush() helper lets the lifespan
  handler in main.py drain the queue cleanly on shutdown.
"""

from typing import Any

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)


def init_langfuse() -> Any:
    """
    Initialise the Langfuse client and return it, or return None if credentials
    are not configured.

    Called once at module load time (the result is stored in the module-level
    `langfuse` variable). Can also be called directly in tests with patched
    settings to verify the initialisation logic without reloading the module.

    Returns:
        A configured Langfuse instance if LANGFUSE_SECRET_KEY and
        LANGFUSE_PUBLIC_KEY are both set; otherwise None.
    """
    if not settings.langfuse_secret_key or not settings.langfuse_public_key:
        logger.info(
            "langfuse not initialised — LANGFUSE_SECRET_KEY or LANGFUSE_PUBLIC_KEY not set"
        )
        return None

    try:
        from langfuse import Langfuse  # type: ignore[import-untyped,unused-ignore]
    except ImportError:
        # langfuse package not installed — treat as disabled.
        logger.warning("langfuse package not installed — tracing disabled")
        return None

    client = Langfuse(
        secret_key=settings.langfuse_secret_key,
        public_key=settings.langfuse_public_key,
        # host defaults to https://cloud.langfuse.com (the SaaS offering).
        # Self-hosted Langfuse: set LANGFUSE_HOST to your server URL.
    )
    logger.info("langfuse initialised", host="https://cloud.langfuse.com")
    return client


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
# Evaluated once at import time. Every module that does
#   from app.langfuse_client import langfuse
# gets this same object (or None if credentials are absent).
#
# We annotate with `Any` here because the Langfuse type comes from an
# optional dependency — mypy can't resolve it without stubs.
langfuse = init_langfuse()


def flush() -> None:
    """
    Drain Langfuse's internal event queue.

    Call this in the application shutdown hook (lifespan handler) to ensure
    all pending trace events are sent before the process exits. Langfuse
    batches spans and sends them in the background — if the process exits
    before the queue drains, the last few traces are lost.

    If Langfuse is disabled (langfuse is None), this is a no-op.
    """
    if langfuse is not None:
        langfuse.flush()
        logger.info("langfuse queue flushed")
