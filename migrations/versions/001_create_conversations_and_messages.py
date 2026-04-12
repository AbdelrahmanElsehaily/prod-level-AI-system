"""create conversations and messages tables

Revision ID: 001
Revises:
Create Date: 2026-04-12

What this migration does
------------------------
Creates the initial schema for storing chat history:

  conversations — one row per chat session (groups messages together)
  messages      — one row per message (user or assistant turn)

Why we write migrations instead of calling Base.metadata.create_all()
----------------------------------------------------------------------
`create_all()` creates tables if they don't exist, but it cannot ALTER
existing tables. If you add a column to your ORM model and call create_all()
again, nothing happens — the table already exists, so it's skipped entirely.

Alembic migration files are versioned scripts that describe EXACTLY what
changed. Each migration has an upgrade() (apply) and downgrade() (reverse).
Alembic tracks which migrations have run in an `alembic_version` table —
running `alembic upgrade head` twice is safe (already-applied revisions skip).

This means your database schema can evolve over time without data loss, and
every environment (dev, staging, production) goes through the same migrations
in the same order — no "works on my machine" schema drift.

upgrade() vs downgrade()
------------------------
Always implement both. downgrade() is what makes `alembic downgrade -1` work,
letting you roll back a bad deployment. If downgrade() just passes, you lose
the ability to roll back — dangerous in production.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Alembic uses these revision IDs to build a linked list of migrations.
# down_revision=None means this is the first migration (no predecessor).
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Apply the migration: create both tables and the message_role enum type.

    Order matters:
      1. Create the ENUM type first — columns that reference it must be created after.
      2. Create `conversations` first — `messages` has a FK that references it.
         Postgres enforces FK integrity at the DDL level, so the referenced table
         must exist before the referencing table is created.
    """

    # --- Step 1: Create the Postgres ENUM type ---
    # Postgres stores ENUM values as integers internally (not strings), which
    # makes them compact and fast to compare. The enum type must be created
    # separately before it can be used as a column type.
    message_role_enum = postgresql.ENUM(
        "user",
        "assistant",
        name="message_role",  # This is the name of the type in Postgres
    )
    message_role_enum.create(op.get_bind())

    # --- Step 2: Create the conversations table ---
    op.create_table(
        "conversations",

        # UUID primary key — random, unguessable, safe for public-facing IDs.
        # server_default uses Postgres's gen_random_uuid() function so even raw
        # SQL inserts (without going through the ORM) get a valid UUID.
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),

        # created_at and updated_at use TIMESTAMP WITH TIME ZONE (timestamptz).
        # Always store timestamps in UTC with timezone info — plain TIMESTAMP
        # (without timezone) is ambiguous and causes bugs when servers change
        # timezone or DST shifts occur.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),

        sa.PrimaryKeyConstraint("id"),
    )

    # --- Step 3: Create the messages table ---
    op.create_table(
        "messages",

        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),

        # Foreign key to conversations.id with CASCADE DELETE:
        # deleting a conversation automatically deletes all its messages.
        # Without CASCADE, you'd have to delete messages manually before
        # deleting the conversation, or the FK constraint would reject the delete.
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),

        # ENUM column — references the type created in Step 1.
        # The `existing_type` argument is required when the type is pre-created.
        sa.Column(
            "role",
            postgresql.ENUM("user", "assistant", name="message_role", create_type=False),
            nullable=False,
        ),

        # TEXT stores unlimited-length strings efficiently in Postgres.
        # VARCHAR(n) would impose an arbitrary length limit that we'd
        # inevitably need to raise later via another migration.
        sa.Column("content", sa.Text(), nullable=False),

        # Nullable: only assistant messages have a token count.
        # User messages are not sent to the AI (they're input), so there
        # are no completion tokens to count for them.
        sa.Column("tokens_used", sa.Integer(), nullable=True),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),

        sa.PrimaryKeyConstraint("id"),
    )

    # Index on conversation_id: we frequently query "all messages in conversation X".
    # Without an index, that query is a full table scan (slow as messages grow).
    # With the index, Postgres goes directly to matching rows — O(log n) lookup.
    op.create_index(
        "ix_messages_conversation_id",
        "messages",
        ["conversation_id"],
    )


def downgrade() -> None:
    """
    Reverse the migration: drop both tables and the enum type.

    Order is the REVERSE of upgrade():
      1. Drop messages first — it has a FK that references conversations.
         Dropping conversations first would fail because messages still
         references it (FK constraint violation).
      2. Drop conversations.
      3. Drop the ENUM type — it can only be dropped after all columns that
         use it are gone.
    """
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_table("conversations")

    # Drop the enum type from Postgres.
    # This must come AFTER dropping the columns/tables that use it.
    postgresql.ENUM(name="message_role").drop(op.get_bind())
