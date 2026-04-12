"""
app/services/chat_history.py — conversation and message persistence
====================================================================
This module owns all reads and writes to the conversations and messages tables.
No other file in the codebase queries those tables directly — they call these
functions instead.

Why centralise DB access in one module?
  If every router queries the DB directly, you end up with the same SQL
  scattered in 5 places. When you need to change "load last 20 messages" to
  "load last 50", you find and edit 5 different spots — and miss one.
  Centralising here means that change is one line in one function.

Async all the way
  Every function here is `async`. This is required because:
    - Our DB driver (asyncpg) is async-only
    - FastAPI runs on an async event loop (uvicorn + anyio)
    - A blocking (sync) DB call inside an async route would freeze the entire
      event loop — no other requests could be handled until the query returned.
  With async functions, the event loop can handle other requests while waiting
  for Postgres to respond.

Function signatures
  Each function takes a `db: AsyncSession` as its first argument.
  The session is created and managed by the dependency in app/dependencies.py
  and injected into routes via Depends(get_db). Services never create their
  own sessions — they receive one from the caller. This makes testing easy:
  pass a mock session, assert what was called.
"""

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Conversation, Message, MessageRole

logger = structlog.get_logger(__name__)


async def create_conversation(db: AsyncSession) -> Conversation:
    """
    Create a new conversation row and return it.

    Called at the start of a chat when no conversation_id is provided by the
    client — i.e. this is the user's first message in a new session.

    The conversation starts empty (no messages). Messages are added separately
    via add_message() as the conversation progresses.

    Args:
        db: AsyncSession injected from app/dependencies.py

    Returns:
        The newly created Conversation ORM object, with id and created_at set.
    """
    conversation = Conversation()  # id and timestamps set by DB server_defaults
    db.add(conversation)

    # flush() sends the INSERT to Postgres within the current transaction
    # WITHOUT committing. This makes the new row visible to subsequent queries
    # in the same session (e.g. if we immediately call add_message() after),
    # while still allowing the whole operation to be rolled back if something
    # fails later.
    # The transaction is committed by FastAPI's dependency cleanup in get_db().
    await db.flush()

    # refresh() re-reads the row from Postgres so that server_default values
    # (id, created_at, updated_at) are populated on the Python object.
    # Without this, conversation.id would still be None even though Postgres
    # generated the UUID.
    await db.refresh(conversation)

    await logger.ainfo(
        "conversation created",
        conversation_id=str(conversation.id),
    )

    return conversation


async def add_message(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    role: MessageRole,
    content: str,
    tokens_used: int | None = None,
) -> Message:
    """
    Append one message to an existing conversation.

    Called twice per chat turn:
      1. After receiving the user's message: role=USER, tokens_used=None
      2. After receiving Claude's reply:     role=ASSISTANT, tokens_used=<count>

    Args:
        db:              AsyncSession from the request's dependency
        conversation_id: UUID of the conversation this message belongs to
        role:            MessageRole.USER or MessageRole.ASSISTANT
        content:         The text of the message
        tokens_used:     Token count from Anthropic's response (assistant only)

    Returns:
        The newly created Message ORM object.
    """
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        tokens_used=tokens_used,
    )
    db.add(message)
    await db.flush()
    await db.refresh(message)

    await logger.ainfo(
        "message saved",
        conversation_id=str(conversation_id),
        role=role.value,
        # Log content length rather than content itself — message content may
        # be sensitive (PII, business data) and should not appear in logs.
        content_length=len(content),
        tokens_used=tokens_used,
    )

    return message


async def get_history(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    limit: int = 20,
) -> list[Message]:
    """
    Return the most recent `limit` messages for a conversation, oldest first.

    Why oldest first?
      The Anthropic API expects messages in chronological order:
        [user: "hello", assistant: "hi!", user: "how are you?", ...]
      Reversing the order here would require re-reversing it before sending
      to the API. Returning chronological order directly is less error-prone.

    Why limit=20?
      Claude's context window is large, but sending 500 old messages in every
      request wastes tokens (money) and slows responses. 20 messages (10 turns)
      gives enough context for coherent conversation without excess cost.
      This can be tuned per use case.

    Args:
        db:              AsyncSession from the request's dependency
        conversation_id: UUID of the conversation to load
        limit:           Max number of messages to return (default 20)

    Returns:
        List of Message ORM objects in chronological order (oldest first).
        Empty list if the conversation has no messages yet.
    """
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        # Order by created_at DESC to get the MOST RECENT messages when we LIMIT.
        # If we ordered ASC and limited to 20, we'd get the first 20 ever sent
        # (the oldest), not the last 20 (the most recent context).
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    messages = result.scalars().all()

    # Reverse to return chronological (oldest first) order for the AI.
    # We fetched newest-first to efficiently get the most recent N messages,
    # now we flip for correct API ordering.
    return list(reversed(messages))
