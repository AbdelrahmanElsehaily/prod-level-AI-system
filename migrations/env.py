"""
migrations/env.py — Alembic environment configuration
=======================================================
This file is the bridge between Alembic and our application. It tells Alembic:
  1. How to connect to the database
  2. What ORM models to inspect when generating migrations
  3. How to run migrations (sync vs async driver)

You generally edit this file once (here) and never touch it again.
Every migration script in versions/ is auto-generated from it.

How Alembic works
-----------------
  alembic revision --autogenerate -m "description"
    → Alembic imports our ORM models (via target_metadata below)
    → Compares them against the CURRENT state of the database
    → Writes a new migration file in migrations/versions/ with the diff
    → You review and commit that file

  alembic upgrade head
    → Runs all pending migration files in order (by revision ID)
    → Records which migrations have run in the `alembic_version` table
    → Idempotent: running it twice is safe, already-applied migrations are skipped

  alembic downgrade -1
    → Reverses the most recent migration (runs its `downgrade()` function)
    → Used to undo a bad deployment or test that rollback works

Async challenge
---------------
Our app uses asyncpg (async Postgres driver) via SQLAlchemy's async engine.
Alembic's standard run_migrations_online() is synchronous and cannot use
an async engine directly. The solution is `AsyncEngine.sync_engine` — a
synchronous view of the async engine that Alembic can use with its normal
connection API. We wrap it in asyncio.run() to execute within the event loop.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
# Import Base (and all models by extension) so Alembic's autogenerate can
# see the full schema. Without this import, `alembic revision --autogenerate`
# would produce empty migration files because it can't find any tables.
from app.models.database import Base

# Alembic Config object — provides access to alembic.ini values
config = context.config

# Set up Python logging from alembic.ini's [loggers] section.
# This gives Alembic's own log output (e.g. "Running upgrade abc → def")
# a consistent format.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# target_metadata tells Alembic what the schema SHOULD look like.
# When you run `alembic revision --autogenerate`, Alembic:
#   1. Connects to the database and inspects its current schema
#   2. Compares it against target_metadata (our ORM models)
#   3. Writes a migration file with the CREATE TABLE / ALTER TABLE / etc.
#      statements needed to bring the database in line with the models
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode — generates SQL without connecting to DB.

    Use case: you want to see what SQL Alembic would run, or you need to
    apply migrations on a database you can't connect to directly (e.g. a
    production DB behind a firewall — generate the SQL, hand it to a DBA).

    Run with: alembic upgrade head --sql
    """
    url = settings.database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    """
    Execute the migration functions within an active DB connection.
    Called by run_migrations_online() with an open synchronous connection.
    """
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        # compare_type=True: detect column TYPE changes during autogenerate.
        # Without this, Alembic won't notice if you change a column from
        # String(100) to Text — it only detects structural changes by default.
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode (the default) — connects to the DB and
    applies migrations directly.

    The async complexity:
      SQLAlchemy's async engine uses asyncpg under the hood, which is an async
      Postgres driver. Alembic expects a synchronous connection for its migration
      API. We bridge this with `engine.sync_engine`, which is a synchronous
      wrapper around the async engine that Alembic can use normally.

      asyncio.run() at the bottom starts the event loop and blocks until
      the migrations complete — exactly what we want for a CLI tool.
    """
    # Create a temporary async engine just for running migrations.
    # We use NullPool (no connection pooling) because migrations run once
    # at startup and then the engine is discarded — no need to keep connections
    # open in a pool.
    connectable = create_async_engine(
        settings.database_url,
        poolclass=pool.NullPool,
    )

    # sync_engine is the synchronous connection interface that Alembic expects.
    # Under the hood it's the same asyncpg connection but exposed synchronously.
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


# Entry point: Alembic calls this file as a script.
# Determine whether we're in offline or online mode and call the right function.
if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
