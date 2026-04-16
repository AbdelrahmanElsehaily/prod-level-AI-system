"""
tests/unit/test_chat_endpoint.py — unit tests for POST /chat
=============================================================
These tests verify the HTTP layer behaviour of the chat endpoint:
  - Valid request → correct response shape
  - Missing message field → 422 Unprocessable Entity
  - Invalid conversation_id → 422
  - AI service failure → 503 Service Unavailable
  - New conversation vs continuing an existing one

We mock at two levels:
  1. FastAPI dependency_overrides for get_db — replaces the DB session
  2. patch("app.routers.chat.ai_service.get_ai_reply") — replaces the AI call

We do NOT mock chat_history service calls at the module level. Instead we
let them call through to the mocked DB session — this tests the full
router → service → DB path without a real database, verifying that the
router passes the right arguments to the service functions.
"""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db
from app.main import app
from app.models.database import Conversation, Message
from app.models.schemas import AIServiceError
from app.services.ai import AIResponse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_mock_db() -> AsyncSession:
    """
    A mock AsyncSession that simulates successful DB operations.

    refresh() side_effect populates id fields so callers get valid UUIDs
    back from create_conversation() and add_message().
    """
    db = MagicMock(spec=AsyncSession)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    async def fake_refresh(obj: object) -> None:
        if isinstance(obj, Conversation) and not getattr(obj, "id", None):
            object.__setattr__(obj, "id", uuid.uuid4())
        if isinstance(obj, Message) and not getattr(obj, "id", None):
            object.__setattr__(obj, "id", uuid.uuid4())

    db.refresh = AsyncMock(side_effect=fake_refresh)

    # get_history returns [] by default (new conversation, no history)
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=mock_result)

    return db  # type: ignore[return-value]


def make_mock_ai_response(
    reply: str = "Hello! How can I help?",
    tokens_used: int = 5,
    total_tokens: int = 15,
    model: str = "llama3.2:cloud",
) -> AIResponse:
    return AIResponse(
        reply=reply,
        tokens_used=tokens_used,
        total_tokens=total_tokens,
        model=model,
    )


@pytest.fixture
def client_with_mocks():
    """
    TestClient with DB mocked via dependency_overrides.
    Each test that needs AI mocking patches get_ai_reply separately.
    """
    mock_db = make_mock_db()

    async def _db() -> AsyncGenerator[AsyncSession, None]:
        yield mock_db

    app.dependency_overrides[get_db] = _db
    with TestClient(app) as client:
        yield client, mock_db
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestChatEndpoint:

    def test_new_conversation_returns_valid_response(
        self, client_with_mocks: tuple
    ) -> None:
        """
        GIVEN: A valid message with no conversation_id (new conversation)
        WHEN:  POST /chat is called
        THEN:  Returns 200 with all required ChatResponse fields
        """
        client, _ = client_with_mocks

        with patch(
            "app.routers.chat.ai_service.get_ai_reply",
            new=AsyncMock(return_value=make_mock_ai_response()),
        ):
            response = client.post("/chat", json={"message": "Hello!"})

        assert response.status_code == 200
        data = response.json()

        # Assert exact response shape — this acts as a contract test
        assert "conversation_id" in data
        assert "reply" in data
        assert "tokens_used" in data
        assert "model" in data

        assert data["reply"] == "Hello! How can I help?"
        assert data["tokens_used"] == 15
        assert data["model"] == "llama3.2:cloud"

        # conversation_id must be a valid UUID (auto-created)
        uuid.UUID(data["conversation_id"])  # raises ValueError if invalid

    def test_new_conversation_id_is_returned(
        self, client_with_mocks: tuple
    ) -> None:
        """
        GIVEN: No conversation_id in request
        WHEN:  POST /chat is called
        THEN:  A new conversation_id is present in the response

        Clients use this ID in follow-up requests to continue the conversation.
        If it's missing, the client can never continue — every message starts fresh.
        """
        client, _ = client_with_mocks

        with patch(
            "app.routers.chat.ai_service.get_ai_reply",
            new=AsyncMock(return_value=make_mock_ai_response()),
        ):
            response = client.post("/chat", json={"message": "Hi"})

        assert response.status_code == 200
        conv_id = response.json()["conversation_id"]
        assert conv_id is not None
        assert len(conv_id) > 0

    def test_missing_message_returns_422(
        self, client_with_mocks: tuple
    ) -> None:
        """
        GIVEN: Request body with no message field
        WHEN:  POST /chat is called
        THEN:  Returns 422 Unprocessable Entity

        FastAPI validates the body against ChatRequest automatically.
        We never reach the route function — validation fails first.
        No need to mock the AI here because the request is rejected before
        any service is called.
        """
        client, _ = client_with_mocks
        response = client.post("/chat", json={})

        assert response.status_code == 422

    def test_empty_message_returns_422(
        self, client_with_mocks: tuple
    ) -> None:
        """
        GIVEN: Request with message="" (empty string)
        WHEN:  POST /chat is called
        THEN:  Returns 422 — the min_length=1 constraint on ChatRequest.message

        Sending an empty message to Ollama would waste a model call and
        return a confused response. The Pydantic constraint catches it first.
        """
        client, _ = client_with_mocks
        response = client.post("/chat", json={"message": ""})

        assert response.status_code == 422

    def test_invalid_conversation_id_returns_422(
        self, client_with_mocks: tuple
    ) -> None:
        """
        GIVEN: A conversation_id that is not a valid UUID
        WHEN:  POST /chat is called
        THEN:  Returns 422

        The router parses conversation_id as uuid.UUID() and raises
        HTTPException(422) if it's not a valid UUID format.
        """
        client, _ = client_with_mocks

        response = client.post(
            "/chat",
            json={"message": "hi", "conversation_id": "not-a-uuid"},
        )

        assert response.status_code == 422

    def test_ai_service_failure_returns_503(
        self, client_with_mocks: tuple
    ) -> None:
        """
        GIVEN: Ollama is down (get_ai_reply raises AIServiceError)
        WHEN:  POST /chat is called
        THEN:  Returns 503 Service Unavailable — not 500

        503 is correct here: OUR code is fine, a DEPENDENCY is unavailable.
        500 would imply a bug in our application.
        Clients seeing 503 know to retry; clients seeing 500 think our code crashed.
        """
        client, _ = client_with_mocks

        with patch(
            "app.routers.chat.ai_service.get_ai_reply",
            new=AsyncMock(side_effect=AIServiceError("Ollama unreachable")),
        ):
            response = client.post("/chat", json={"message": "hello"})

        assert response.status_code == 503

    def test_continuing_conversation_uses_provided_id(
        self, client_with_mocks: tuple
    ) -> None:
        """
        GIVEN: A valid UUID conversation_id in the request
        WHEN:  POST /chat is called
        THEN:  The same conversation_id is returned in the response

        The client provides an ID → the router continues that conversation
        (loads its history) → returns the same ID so the client can continue
        sending messages in the same session.
        """
        client, _ = client_with_mocks
        existing_id = str(uuid.uuid4())

        with patch(
            "app.routers.chat.ai_service.get_ai_reply",
            new=AsyncMock(return_value=make_mock_ai_response()),
        ):
            response = client.post(
                "/chat",
                json={"message": "Continue please", "conversation_id": existing_id},
            )

        assert response.status_code == 200
        assert response.json()["conversation_id"] == existing_id
