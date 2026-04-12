"""
tests/unit/test_chat_history.py — unit tests for chat_history service
=======================================================================
These tests verify the behaviour of the three service functions:
  - create_conversation()
  - add_message()
  - get_history()

We use MagicMock to simulate the AsyncSession — no real database needed.
This lets us verify that the service calls the right DB methods with the
right arguments, fast and in isolation.

What unit tests prove here:
  - The service adds the correct ORM object to the session
  - flush() and refresh() are always called (not skipped, not called twice)
  - get_history() reverses the query result to return chronological order
  - tokens_used is passed correctly for assistant messages
  - tokens_used is None for user messages

What unit tests do NOT prove here (left to integration tests):
  - The actual SQL query is correct
  - The FK constraint is enforced
  - The ORDER BY in get_history() works in Postgres
  - The ENUM constraint rejects invalid role values

Mocking strategy
----------------
We use unittest.mock.MagicMock with spec=AsyncSession so that:
  1. Attribute access is validated against the real AsyncSession interface —
     typos like `db.flosh()` fail immediately instead of silently succeeding.
  2. Async methods (execute, flush, refresh) are replaced with AsyncMock so
     they can be awaited in async test functions.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Conversation, Message, MessageRole
from app.services.chat_history import add_message, create_conversation, get_history


def make_mock_db() -> AsyncSession:
    """
    Build a mock AsyncSession with all async methods stubbed out.

    We pre-configure refresh() to populate the ORM object's id field —
    normally Postgres does this via server_default, but since we're not
    hitting a real DB, we simulate it by setting the attribute in the
    side_effect of refresh().
    """
    db = MagicMock(spec=AsyncSession)
    db.add = MagicMock()          # synchronous — just registers the object
    db.flush = AsyncMock()        # async — sends INSERT without committing
    db.execute = AsyncMock()      # async — runs a SELECT
    db.refresh = AsyncMock()      # async — re-reads the row from DB

    # Simulate what Postgres does on refresh: populate server-generated fields.
    # side_effect receives the argument passed to refresh() (the ORM object)
    # and sets its id if it's not already set. This lets us assert on obj.id.
    async def fake_refresh(obj: object) -> None:
        if isinstance(obj, (Conversation, Message)) and obj.id is None:
            object.__setattr__(obj, "id", uuid.uuid4())

    db.refresh.side_effect = fake_refresh
    return db  # type: ignore[return-value]


class TestCreateConversation:

    @pytest.mark.asyncio
    async def test_returns_conversation_object(self) -> None:
        """
        GIVEN: A valid DB session
        WHEN:  create_conversation() is called
        THEN:  It returns a Conversation ORM object
        """
        db = make_mock_db()
        result = await create_conversation(db)
        assert isinstance(result, Conversation)

    @pytest.mark.asyncio
    async def test_adds_conversation_to_session(self) -> None:
        """
        GIVEN: A valid DB session
        WHEN:  create_conversation() is called
        THEN:  db.add() is called with the new Conversation object

        Why test this? If someone removes `db.add(conversation)`, the row is
        never queued for INSERT — it silently disappears. This test catches that.
        """
        db = make_mock_db()
        result = await create_conversation(db)
        db.add.assert_called_once_with(result)

    @pytest.mark.asyncio
    async def test_flushes_and_refreshes(self) -> None:
        """
        GIVEN: A valid DB session
        WHEN:  create_conversation() is called
        THEN:  flush() then refresh() are called exactly once, in that order

        flush() sends the INSERT to Postgres. refresh() reads back server_default
        values (the UUID and timestamps). If either is skipped, the returned
        object has missing/stale data. The ORDER also matters — refreshing before
        flushing would read a row that doesn't exist yet.
        """
        db = make_mock_db()
        result = await create_conversation(db)

        db.flush.assert_awaited_once()
        db.refresh.assert_awaited_once_with(result)

        # Verify order: flush must be called before refresh.
        # call_args_list records every call in order.
        flush_pos = next(
            i for i, c in enumerate(db.method_calls) if c == call.flush()
        )
        refresh_pos = next(
            i for i, c in enumerate(db.method_calls) if "refresh" in str(c)
        )
        assert flush_pos < refresh_pos, "flush() must be called before refresh()"


class TestAddMessage:

    @pytest.mark.asyncio
    async def test_user_message_has_no_tokens(self) -> None:
        """
        GIVEN: A user turn message (no AI involved)
        WHEN:  add_message() is called without tokens_used
        THEN:  The stored Message has tokens_used=None

        User messages are input to the AI — the AI hasn't responded yet,
        so there are no output tokens to count. Storing tokens_used=None
        correctly represents this (vs storing 0, which implies the AI responded
        with an empty message).
        """
        db = make_mock_db()
        conv_id = uuid.uuid4()

        msg = await add_message(
            db=db,
            conversation_id=conv_id,
            role=MessageRole.USER,
            content="hello there",
            # tokens_used intentionally omitted — defaults to None
        )

        assert msg.tokens_used is None
        assert msg.role == MessageRole.USER
        assert msg.content == "hello there"
        assert msg.conversation_id == conv_id

    @pytest.mark.asyncio
    async def test_assistant_message_stores_tokens(self) -> None:
        """
        GIVEN: An assistant reply with a known token count
        WHEN:  add_message() is called with tokens_used=42
        THEN:  The stored Message has tokens_used=42

        Token counts are used for cost tracking (each token costs money) and
        for Langfuse tracing (Step 9). If we lose the count here, we can't
        reconstruct it later.
        """
        db = make_mock_db()
        conv_id = uuid.uuid4()

        msg = await add_message(
            db=db,
            conversation_id=conv_id,
            role=MessageRole.ASSISTANT,
            content="Hello! How can I help?",
            tokens_used=42,
        )

        assert msg.tokens_used == 42
        assert msg.role == MessageRole.ASSISTANT

    @pytest.mark.asyncio
    async def test_message_is_added_flushed_and_refreshed(self) -> None:
        """
        GIVEN: Any message
        WHEN:  add_message() is called
        THEN:  add(), flush(), refresh() all called once in order
        """
        db = make_mock_db()
        msg = await add_message(
            db=db,
            conversation_id=uuid.uuid4(),
            role=MessageRole.USER,
            content="test",
        )
        db.add.assert_called_once_with(msg)
        db.flush.assert_awaited_once()
        db.refresh.assert_awaited_once_with(msg)


class TestGetHistory:

    @pytest.mark.asyncio
    async def test_returns_messages_in_chronological_order(self) -> None:
        """
        GIVEN: A DB that returns [newest, middle, oldest] (DESC query order)
        WHEN:  get_history() is called
        THEN:  It returns [oldest, middle, newest] (chronological / ASC order)

        The Anthropic API requires messages in chronological order. The service
        queries newest-first (to efficiently get the most recent N with LIMIT),
        then reverses. If the reversal is missing, the AI receives context
        backwards — it would see the most recent message as the "first" one.
        """
        db = make_mock_db()
        conv_id = uuid.uuid4()

        # Simulate what Postgres returns: newest first (DESC order)
        newest = MagicMock(spec=Message, created_at="2024-01-01 12:00:02")
        middle = MagicMock(spec=Message, created_at="2024-01-01 12:00:01")
        oldest = MagicMock(spec=Message, created_at="2024-01-01 12:00:00")

        # db.execute() returns a result; .scalars().all() returns the list.
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [newest, middle, oldest]
        db.execute.return_value = mock_result

        history = await get_history(db, conv_id)

        # Expect chronological order (oldest first) — reversed from the query
        assert history == [oldest, middle, newest]

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_new_conversation(self) -> None:
        """
        GIVEN: A conversation with no messages
        WHEN:  get_history() is called
        THEN:  Returns an empty list (not None, not an error)

        The chat endpoint calls get_history() before every message. For a brand
        new conversation (first user message), the history must be an empty list
        — passing None or raising an exception would crash the AI call.
        """
        db = make_mock_db()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        db.execute.return_value = mock_result

        history = await get_history(db, uuid.uuid4())
        assert history == []
        assert isinstance(history, list)
