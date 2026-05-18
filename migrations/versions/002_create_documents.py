"""create documents table

Revision ID: 002
Revises: 001
Create Date: 2026-05-18

What this migration does
------------------------
Creates the `documents` table for Step 13 (Document Q&A with dspy.RLM).

  documents — one row per uploaded file. Stores the parsed plain-text
              content (`content_text`) so the RLM can load it into its
              Pyodide sandbox at query time. The original binary file is
              NOT stored — we keep only the extracted text, which is what
              the model actually reads.

Why no `chunks` table or vector column?
  Unlike classic RAG, dspy.RLM does not pre-chunk or pre-embed documents.
  The model loads the full text into a sandboxed Python REPL and
  navigates it itself (grep, slice, sub-LM calls). So storage is just
  "filename → big string". Simpler schema, no pgvector dependency.

Idempotency
-----------
We do not use server_default `gen_random_uuid()` on the id column on
purpose — the application generates the UUID in Python via the model's
default=uuid.uuid4. This matches the convention used by conversations/
messages and keeps the migration agnostic to the Postgres version
(pgcrypto / gen_random_uuid may or may not be available).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Linked to migration 001 (conversations + messages).
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Create the documents table.

    Columns:
      id           — UUID primary key, generated app-side.
      filename     — original filename the user uploaded (display only).
      content_type — MIME type sniffed at upload (text/plain, application/pdf, ...).
      content_text — the FULL extracted text. TEXT (unlimited length). This is
                     what dspy.RLM loads into the Pyodide sandbox at query time.
                     Enforced max size lives in the application layer, not here.
      size_bytes   — original file size before extraction. Useful for limits and
                     for showing the user "you have used X of Y MB".
      created_at   — when the upload happened.
    """
    op.create_table(
        "documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        # TEXT, not VARCHAR(N). A docx may extract to hundreds of KB; we should
        # not pre-pick a max. The app-layer upload limit (default 1 MB raw input)
        # is the practical guardrail.
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Index on created_at so "GET /documents" (newest first) is an index scan.
    op.create_index(
        "ix_documents_created_at",
        "documents",
        ["created_at"],
    )


def downgrade() -> None:
    """Drop the documents table. Index goes with it."""
    op.drop_index("ix_documents_created_at", table_name="documents")
    op.drop_table("documents")
