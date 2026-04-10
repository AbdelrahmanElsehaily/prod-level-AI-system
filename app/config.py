"""
app/config.py — centralised settings management
================================================
All configuration for this application lives here and ONLY here.
No other file should call os.getenv() or read environment variables directly.

Why: if env vars are read in 20 different places, it's nearly impossible to
know which ones exist, what type they should be, or what happens if they are
missing. Centralising them here means one place to look, one place to validate.

We use Pydantic's BaseSettings, which:
  1. Reads values from environment variables automatically (by field name)
  2. Validates types — e.g. if DATABASE_URL is missing, startup fails immediately
     with a clear error instead of a cryptic crash later
  3. Supports .env files in development via python-dotenv (included in pydantic-settings)
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Application identity ---
    # Controls log format (json in production, pretty-print in development)
    # and which Sentry environment tag is used.
    environment: str = "development"

    # Semantic version of this app, used in /health response so you can
    # see at a glance which version is running on each environment.
    version: str = "0.1.0"

    # --- External services ---
    # asyncpg is the async Postgres driver; SQLAlchemy uses it via the
    # "postgresql+asyncpg://" scheme.
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/chatapi"
    )

    # Redis is used for rate limiting (Step 6) and response caching (Step 12).
    redis_url: str = "redis://localhost:6379"

    # --- AI provider ---
    # Required in production. We use | None so startup doesn't crash in local
    # dev if you haven't set this yet — the AI endpoint will fail at call-time
    # with a clear error instead.
    anthropic_api_key: str | None = None

    # --- Observability ---
    # Optional — Sentry is only initialised if this is set (Step 8).
    sentry_dsn: str | None = None

    # Optional — Langfuse is only initialised if this is set (Step 9).
    langfuse_secret_key: str | None = None
    langfuse_public_key: str | None = None

    # SettingsConfigDict tells Pydantic where to look for values:
    #   env_file=".env"  — load a local .env file if present (for local dev)
    #   extra="ignore"   — silently ignore any extra env vars we don't declare
    #                      (Docker and Railway inject many vars we don't need)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# Module-level singleton — import and use this everywhere.
# Calling Settings() reads and validates all env vars once at import time.
# If something is wrong (e.g. a required var is missing), the app fails fast
# at startup rather than in the middle of handling a request.
settings = Settings()
