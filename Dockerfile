# syntax=docker/dockerfile:1
# ^^^ Enables BuildKit features. Always include this as the first line.

# ---------------------------------------------------------------------------
# Stage 1: dependency installer
# ---------------------------------------------------------------------------
# We use uv's official image to get the uv binary, then use it to install
# all project dependencies into a virtual environment at /app/.venv.
#
# Why a separate stage for deps?
#   Docker caches each layer. If we copy ALL files first and then install deps,
#   any code change invalidates the dep-install layer (slow, ~60s).
#   By copying ONLY the lockfile and pyproject.toml first, the dep layer is
#   only invalidated when dependencies actually change — not on every code edit.
#
# Why uv instead of pip?
#   - 10-100x faster installs (Rust-based resolver)
#   - Reads uv.lock for exact reproducible versions — same packages in every
#     environment: local dev, CI, Docker, Railway
#   - Manages the virtualenv automatically; no manual `python -m venv` needed
FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

FROM python:3.12-slim AS deps

WORKDIR /app

# Copy uv binary from its official image into this stage.
# Using COPY --from instead of RUN curl/apt-get keeps this layer tiny.
COPY --from=uv /uv /usr/local/bin/uv

# Copy ONLY the files uv needs to resolve and install dependencies.
# These rarely change, so Docker caches this layer aggressively.
# Copying them before the rest of the source code means a code change does NOT
# invalidate this layer — deps are reinstalled only when pyproject.toml or
# uv.lock actually changes.
COPY pyproject.toml uv.lock .python-version ./

# Install all dependencies (runtime + dev) into /app/.venv.
#
# --frozen         → use uv.lock exactly as-is; fail if it's out of sync with
#                    pyproject.toml. Prevents "works in CI but fails in Docker"
#                    due to a lockfile that wasn't regenerated after a dep change.
# --all-extras     → install the [dev] group (pytest etc.) — useful for
#                    running tests inside the container in CI
# --no-install-project → install deps but NOT the app itself yet; the app
#                        code comes in the next COPY below, keeping layers clean
RUN uv sync --frozen --all-extras --no-install-project

# ---------------------------------------------------------------------------
# Stage 2: final runtime image
# ---------------------------------------------------------------------------
# Copy the installed venv and the application code into a clean slim image.
# We do NOT copy uv itself here — it is a build-time tool, not needed at runtime.
FROM python:3.12-slim AS final

WORKDIR /app

# Install curl so the Docker HEALTHCHECK (curl -sf http://localhost:8000/health)
# and any CI smoke-test scripts can reach the app.
#
# Why curl and not wget or Python urllib?
#   curl is the de-facto tool used in most healthcheck one-liners and is what
#   our docker-compose.test.yml already uses. Keeping the same tool avoids
#   two different HTTP clients in the stack.
#
# Why --no-install-recommends?
#   apt would otherwise pull in dozens of optional packages (documentation,
#   locale data, …). --no-install-recommends limits the install to curl and
#   its hard runtime deps only, keeping the image as slim as possible.
#
# Why rm -rf /var/lib/apt/lists/*?
#   After apt finishes it leaves a cache of package index files in
#   /var/lib/apt/lists/. That cache is only needed during the RUN step — once
#   the layer is committed we never need it again. Deleting it in the SAME RUN
#   instruction (important! a separate RUN would create a new layer on top of
#   the old one without actually shrinking the image) saves ~30 MB.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-built virtual environment from the deps stage.
# This avoids re-running uv sync in the final image, keeping it clean and fast.
COPY --from=deps /app/.venv /app/.venv

# Add the venv's bin/ to PATH so `python` and `uvicorn` resolve to the venv's
# versions, not the system Python — no need to prefix every command with
# `/app/.venv/bin/`.
ENV PATH="/app/.venv/bin:$PATH"

# Tell Python where to find our application package.
#
# Why is this needed?
#   When uvicorn starts via CMD, Python automatically adds the working directory
#   (/app) to sys.path — so `import app` resolves to /app/app/ correctly.
#   BUT when Railway's startCommand runs `alembic upgrade head` first (before
#   uvicorn ever starts), alembic launches as a plain subprocess. Plain subprocess
#   calls do NOT add CWD to sys.path automatically, so migrations/env.py's
#   `from app.config import settings` raises:
#     ModuleNotFoundError: No module named 'app'
#
#   Setting PYTHONPATH="/app" makes /app a permanent entry in sys.path for every
#   process in this container — alembic, uvicorn, and any future scripts — so
#   `import app` always resolves correctly regardless of how the process started.
ENV PYTHONPATH="/app"

# Copy application source code.
# This layer changes on every code edit — but that's fine because it's last.
# Docker only re-runs layers that changed AND every layer after them.
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini ./

EXPOSE 8000

# Run the app using uvicorn from the venv (via PATH above).
# --host 0.0.0.0 makes the server reachable from outside the container.
# --port 8000 matches the EXPOSE above and the docker-compose port mapping.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
