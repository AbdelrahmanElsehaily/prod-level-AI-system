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
  2. Validates types — if a required var is missing, startup fails immediately
     with a clear error instead of a cryptic crash deep inside a request
  3. Supports .env files in development via python-dotenv (bundled with pydantic-settings)
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Application identity ---
    # Controls log format (json in production, pretty-print in development)
    environment: str = "development"

    # Semantic version shown in /health and /docs
    version: str = "0.1.0"

    # --- Database ---
    # asyncpg is the async Postgres driver; SQLAlchemy uses it via the
    # "postgresql+asyncpg://" scheme.
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/chatapi"
    )

    # --- Cache / rate limiting ---
    # Redis is used for rate limiting (Step 6) and response caching (Step 12).
    redis_url: str = "redis://localhost:6379"

    # --- AI provider: Ollama ---
    # Ollama runs open-source LLMs locally (Llama, Mistral, Gemma, etc.).
    # It exposes a REST API — no cloud account, no API key, no usage costs.
    #
    # ollama_base_url: where the Ollama server is listening.
    #   - Local dev:   http://localhost:11434   (default)
    #   - Docker:      http://host.docker.internal:11434
    #                  (host.docker.internal resolves to your Mac's localhost
    #                   from inside a Docker container — needed because
    #                   "localhost" inside a container means the container itself,
    #                   not the host machine where Ollama is running)
    #   - Remote:      http://<server-ip>:11434
    ollama_base_url: str = "http://localhost:11434"

    # ollama_model: which model to run. Must be pulled first with:
    #   ollama pull llama3.2
    # Any model available in `ollama list` works here.
    # Override per-environment without touching code:
    #   OLLAMA_MODEL=mistral docker compose up
    ollama_model: str = "llama3.2"

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


# Module-level singleton — import this everywhere instead of calling os.getenv().
# Pydantic validates all fields at import time. If something is wrong
# (wrong type, missing required field), the app crashes immediately at startup
# with a clear validation error — not silently mid-request.
settings = Settings()
