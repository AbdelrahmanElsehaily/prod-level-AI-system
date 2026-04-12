"""
app/models/
===========
This package contains two kinds of models:

  database.py  — SQLAlchemy ORM classes that map to Postgres tables.
                 These are the source of truth for the database schema.
                 Alembic reads them to auto-generate migration files.

  schemas.py   — Pydantic models for HTTP request/response validation.
                 These are what FastAPI uses to parse request bodies and
                 serialise response bodies. Added in Step 5.

Keeping them separate avoids a common mistake: using the same model class
for both database rows and HTTP payloads. They have different concerns:
  - DB models care about column types, indexes, foreign keys, nullability
  - HTTP schemas care about validation rules, field aliases, documentation
Mixing them forces awkward compromises in both directions.
"""
