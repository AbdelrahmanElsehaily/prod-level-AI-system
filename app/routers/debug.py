"""
app/routers/debug.py — development-only debug endpoints
=========================================================
These routes exist ONLY to verify integrations work in a real deployed
environment. They are mounted conditionally in main.py — only when
ENVIRONMENT is not "production".

Why a separate router file instead of inline in main.py?
  Keeping debug routes in their own file makes it obvious they are not
  production code. It also keeps main.py clean — the conditional mount
  is one line, not a wall of route definitions.

Why not just delete these routes before deploying?
  Deleting means you'd have to re-add them every time you want to test
  a new integration in staging. The environment guard is safer:
    - Production:  routes never mounted → 404, no risk
    - Staging/dev: routes mounted → useful for verification
  The guard is enforced by the settings object which reads ENVIRONMENT
  from the environment variable — not from a flag that could be forgotten.

Routes
------
GET /debug/error
  Deliberately raises an unhandled exception. Use this to verify that
  Sentry is receiving events from a deployed environment. Hit the endpoint,
  then check your Sentry dashboard for the event within ~5 seconds.

  Expected response: HTTP 500 (FastAPI's default unhandled exception response)
  Expected Sentry event: ValueError with message "intentional test error"
"""

from fastapi import APIRouter

router = APIRouter(prefix="/debug", tags=["Debug"])


@router.get(
    "/error",
    summary="Trigger a test error (non-production only)",
    description=(
        "Raises an intentional exception to verify Sentry is capturing errors. "
        "Only available in non-production environments."
    ),
)
async def trigger_error() -> dict[str, str]:
    """
    Raise a deliberate unhandled exception.

    FastAPI's exception handler will catch this, return HTTP 500, and — if
    Sentry is initialised — send the event to the Sentry dashboard.

    Use this to confirm your SENTRY_DSN is correct and events are flowing
    after a fresh deployment to staging. Check the Sentry dashboard within
    a few seconds of hitting this endpoint.
    """
    raise ValueError(
        "Intentional test error — verifying Sentry event capture. "
        "If you see this in Sentry, the integration is working correctly."
    )
