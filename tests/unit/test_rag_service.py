"""
tests/unit/test_rag_service.py — unit tests for the dspy.RLM wrapper
=====================================================================
Tests cover:
  - ValueError when called with no documents (router contract)
  - _build_corpus produces parseable delimiters
  - ask_documents wires args through to dspy.RLM and unpacks Prediction
  - Usage / iteration extraction handles missing fields gracefully

We DO NOT spin up the real Pyodide sandbox in unit tests. Pyodide needs
Deno installed, and a real LLM call costs money and time. We patch
`dspy.RLM` at the import site (app.services.rag) so the test substitutes
a fake module that returns a fixed Prediction.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.models.database import Document
from app.services import rag as rag_service


def _make_doc(text: str, filename: str = "f.txt") -> Document:
    """Build a Document ORM-like object without touching the DB."""
    doc = MagicMock(spec=Document)
    doc.id = "00000000-0000-0000-0000-000000000001"
    doc.filename = filename
    doc.content_text = text
    return doc


# ---------------------------------------------------------------------------
# _build_corpus
# ---------------------------------------------------------------------------


class TestBuildCorpus:
    def test_includes_each_documents_text(self) -> None:
        docs = [_make_doc("alpha text"), _make_doc("bravo text", filename="b.txt")]
        corpus = rag_service._build_corpus(docs)
        assert "alpha text" in corpus
        assert "bravo text" in corpus

    def test_uses_clear_boundary(self) -> None:
        """Boundary markers must be distinctive enough that the LM can split on them."""
        corpus = rag_service._build_corpus([_make_doc("hi")])
        assert "===== DOCUMENT" in corpus
        assert "===== END DOCUMENT" in corpus

    def test_empty_iterable_returns_empty(self) -> None:
        assert rag_service._build_corpus([]) == ""


# ---------------------------------------------------------------------------
# ask_documents
# ---------------------------------------------------------------------------


class TestAskDocuments:
    async def test_rejects_empty_documents(self) -> None:
        with pytest.raises(ValueError, match="at least one document"):
            await rag_service.ask_documents("anything", [])

    async def test_returns_answer_from_rlm(self) -> None:
        """ask_documents must build an RLM, call it, and unpack the result."""
        fake_prediction = MagicMock()
        fake_prediction.answer = "42"
        fake_prediction.iterations = 3
        fake_prediction.get_lm_usage.return_value = {
            "ollama_chat/test-model": {"total_tokens": 123}
        }

        fake_rlm_instance = MagicMock(return_value=fake_prediction)
        fake_rlm_class = MagicMock(return_value=fake_rlm_instance)

        # Also patch _ensure_lm_configured so we don't try to talk to a real LM.
        with (
            patch.object(rag_service, "dspy") as mock_dspy,
            patch.object(rag_service, "_ensure_lm_configured"),
        ):
            mock_dspy.RLM = fake_rlm_class
            result = await rag_service.ask_documents(
                question="what is the answer?",
                documents=[_make_doc("the answer is 42")],
            )

        # We built one RLM with the documented signature.
        fake_rlm_class.assert_called_once()
        args, kwargs = fake_rlm_class.call_args
        assert args[0] == "context, question -> answer"
        # And we called it with the right inputs.
        fake_rlm_instance.assert_called_once()
        call_kwargs = fake_rlm_instance.call_args.kwargs
        assert call_kwargs["question"] == "what is the answer?"
        assert "42" in call_kwargs["context"]

        assert result.answer == "42"
        assert result.iterations == 3
        assert result.total_tokens == 123

    async def test_handles_missing_usage_gracefully(self) -> None:
        """get_lm_usage() may return None/{}; we must not crash."""
        fake_prediction = MagicMock()
        fake_prediction.answer = "ok"
        # No `iterations` attribute on this prediction.
        del fake_prediction.iterations
        fake_prediction.get_lm_usage.return_value = None

        fake_rlm_instance = MagicMock(return_value=fake_prediction)
        fake_rlm_class = MagicMock(return_value=fake_rlm_instance)

        with (
            patch.object(rag_service, "dspy") as mock_dspy,
            patch.object(rag_service, "_ensure_lm_configured"),
        ):
            mock_dspy.RLM = fake_rlm_class
            result = await rag_service.ask_documents("q", [_make_doc("x")])

        assert result.answer == "ok"
        assert result.iterations == 0
        assert result.total_tokens == 0
