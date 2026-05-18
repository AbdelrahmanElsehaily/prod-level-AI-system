"""
tests/unit/test_documents_service.py — unit tests for document storage
========================================================================
Tests cover:
  - _detect_content_type: trust declared type, fall back to extension, reject unknown
  - _extract_text: txt/md UTF-8, PDF via pypdf (mocked), empty PDF rejected upstream
  - save_document: size cap, type rejection, corpus cap, empty-text rejection
  - delete_document: NotFound when row missing
  - get_documents_by_ids: empty list short-circuits

No real Postgres needed — we MagicMock(spec=AsyncSession) and stub the
specific await calls each test cares about. Same pattern as test_chat_history.
"""

import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import documents as docs_service
from app.services.documents import (
    DocumentNotFoundError,
    DocumentTooLargeError,
    UnsupportedDocumentTypeError,
    _detect_content_type,
    _extract_text,
)

# ---------------------------------------------------------------------------
# _detect_content_type
# ---------------------------------------------------------------------------


class TestDetectContentType:
    def test_trusts_declared_supported_text(self) -> None:
        assert _detect_content_type("a.txt", "text/plain") == "text/plain"
        assert _detect_content_type("a.md", "text/markdown") == "text/markdown"

    def test_trusts_declared_pdf(self) -> None:
        assert _detect_content_type("a.pdf", "application/pdf") == "application/pdf"

    def test_falls_back_to_pdf_extension(self) -> None:
        assert _detect_content_type("file.pdf", None) == "application/pdf"
        # Even if client lies with octet-stream, we trust the extension
        assert (
            _detect_content_type("file.pdf", "application/octet-stream")
            == "application/pdf"
        )

    def test_falls_back_to_text_extension(self) -> None:
        assert _detect_content_type("notes.txt", None) == "text/plain"
        assert _detect_content_type("README.md", None) == "text/markdown"

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(UnsupportedDocumentTypeError):
            _detect_content_type("malware.exe", "application/x-msdownload")

    def test_unknown_extension_rejected(self) -> None:
        with pytest.raises(UnsupportedDocumentTypeError):
            _detect_content_type("script.sh", None)


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------


class TestExtractText:
    def test_text_decoded_as_utf8(self) -> None:
        result = _extract_text("text/plain", "héllo".encode())
        assert result == "héllo"

    def test_invalid_utf8_replaced_not_raised(self) -> None:
        """A single bad byte should NOT kill the whole upload."""
        # \xff is invalid utf-8 in isolation. errors="replace" → U+FFFD.
        result = _extract_text("text/plain", b"good\xfftext")
        assert "good" in result
        assert "text" in result

    def test_pdf_extracts_text(self) -> None:
        """Build a tiny real PDF in memory and extract from it."""
        # Easiest "real PDF" path: use pypdf to construct one. If we mocked
        # PdfReader entirely we'd test our mock, not our integration.
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        # Blank page has no text, so we can't assert on content, but the
        # important thing is the function returns a string and does not raise.
        buf = io.BytesIO()
        writer.write(buf)
        result = _extract_text("application/pdf", buf.getvalue())
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# save_document
# ---------------------------------------------------------------------------


def _mock_db_with_corpus_size(current_bytes: int) -> AsyncMock:
    """Build an AsyncSession mock where _total_stored_bytes returns the given value."""
    db = MagicMock(spec=AsyncSession)
    # _total_stored_bytes does db.execute(SELECT COALESCE(SUM(...)))
    # and reads scalar_one(). We mock that chain.
    scalar_result = MagicMock()
    scalar_result.scalar_one.return_value = current_bytes
    db.execute = AsyncMock(return_value=scalar_result)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return db


class TestSaveDocument:
    async def test_rejects_oversize_file(self) -> None:
        db = _mock_db_with_corpus_size(0)
        with pytest.raises(DocumentTooLargeError, match="per file"):
            await docs_service.save_document(
                db,
                filename="big.txt",
                content_type="text/plain",
                raw=b"x" * (docs_service.MAX_FILE_BYTES + 1),
            )
        # We must not have started writing on a rejected file.
        db.add.assert_not_called()
        db.commit.assert_not_called()

    async def test_rejects_unsupported_type(self) -> None:
        db = _mock_db_with_corpus_size(0)
        with pytest.raises(UnsupportedDocumentTypeError):
            await docs_service.save_document(
                db,
                filename="malware.exe",
                content_type="application/x-msdownload",
                raw=b"hi",
            )
        db.add.assert_not_called()

    async def test_rejects_when_corpus_full(self) -> None:
        # Corpus is already at the cap; any file pushes us over.
        db = _mock_db_with_corpus_size(docs_service.MAX_CORPUS_BYTES)
        with pytest.raises(DocumentTooLargeError, match="max total"):
            await docs_service.save_document(
                db,
                filename="a.txt",
                content_type="text/plain",
                raw=b"hello",
            )

    async def test_rejects_empty_extraction(self) -> None:
        """A non-empty file that extracts to zero text (image-only PDF)."""
        db = _mock_db_with_corpus_size(0)
        with patch.object(docs_service, "_extract_text", return_value="   \n  "):
            with pytest.raises(UnsupportedDocumentTypeError, match="extract any text"):
                await docs_service.save_document(
                    db,
                    filename="scan.pdf",
                    content_type="application/pdf",
                    raw=b"%PDF-1.4 fake",
                )

    async def test_happy_path_writes_document(self) -> None:
        db = _mock_db_with_corpus_size(0)
        raw = b"hello world"

        doc = await docs_service.save_document(
            db,
            filename="hello.txt",
            content_type="text/plain",
            raw=raw,
        )

        assert doc.filename == "hello.txt"
        assert doc.content_type == "text/plain"
        assert doc.content_text == "hello world"
        assert doc.size_bytes == len(raw)
        db.add.assert_called_once()
        db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# delete_document
# ---------------------------------------------------------------------------


class TestDeleteDocument:
    async def test_raises_when_not_found(self) -> None:
        db = MagicMock(spec=AsyncSession)
        # delete returned 0 rows.
        result = MagicMock()
        result.rowcount = 0
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        with pytest.raises(DocumentNotFoundError):
            await docs_service.delete_document(db, uuid.uuid4())
        # No commit on a no-op.
        db.commit.assert_not_called()

    async def test_commits_on_success(self) -> None:
        db = MagicMock(spec=AsyncSession)
        result = MagicMock()
        result.rowcount = 1
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        await docs_service.delete_document(db, uuid.uuid4())
        db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# get_documents_by_ids
# ---------------------------------------------------------------------------


class TestGetDocumentsByIds:
    async def test_empty_list_returns_empty_without_db(self) -> None:
        """Avoid a no-op SQL round-trip when the caller passes []."""
        db = MagicMock(spec=AsyncSession)
        db.execute = AsyncMock()  # would track calls if invoked
        result = await docs_service.get_documents_by_ids(db, [])
        assert result == []
        db.execute.assert_not_called()


# Note: pytest-asyncio is configured in pyproject.toml as asyncio_mode = "auto".
# Async tests are detected automatically; sync tests run sync. No marker needed.
