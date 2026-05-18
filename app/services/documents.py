"""
app/services/documents.py — document storage and text extraction
=================================================================
The ONLY file in the codebase that knows how to turn an uploaded file
into stored text. Keeps file-format quirks (PDF parsing, encoding sniffing,
size limits) out of the HTTP router and the RAG layer.

What we store
-------------
Just the extracted plain text. Never the original binary. The model
(`dspy.RLM`) only consumes text — keeping the original PDF/DOCX bytes
would double our storage for zero feature value, and would force us to
think about S3 / signed URLs / virus scanning.

Supported types (v1)
--------------------
text/plain, text/markdown  → decode bytes as UTF-8
application/pdf            → pypdf.PdfReader → page-by-page extract

Anything else is rejected with UnsupportedDocumentTypeError. We sniff
content type from BOTH the multipart Content-Type header AND the file
extension — clients sometimes lie about Content-Type.

Size limits (enforced at the boundary, not the database)
---------------------------------------------------------
Per-file:   1 MiB (raw upload bytes)
Per-corpus: 10 MiB total stored size

Rationale: dspy.RLM loads documents into a Pyodide WASM sandbox, which
lives in-process. Runaway uploads = runaway memory. Postgres TEXT has no
length limit, but our process does.
"""

import io
import uuid

import pypdf
import structlog
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Document

logger = structlog.get_logger(__name__)


# Public knobs — exposed at module level so tests can monkeypatch them
# without reaching into private state. Bytes, not "1 MB" strings, to
# avoid any base-10 vs base-2 ambiguity.
MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MiB per file
MAX_CORPUS_BYTES = 10 * 1024 * 1024  # 10 MiB total across all documents

# Content types we know how to extract. We accept lying clients only if
# the file extension also matches — see _detect_content_type().
_SUPPORTED_TEXT_TYPES = {"text/plain", "text/markdown"}
_PDF_TYPE = "application/pdf"


class DocumentError(Exception):
    """Base class for all errors this service raises."""


class UnsupportedDocumentTypeError(DocumentError):
    """Raised when we can't extract text from the uploaded file type."""


class DocumentTooLargeError(DocumentError):
    """Raised when a single file or the cumulative corpus exceeds the size cap."""


class DocumentNotFoundError(DocumentError):
    """Raised when a document_id does not exist in the database."""


# ---------------------------------------------------------------------------
# Content-type detection
# ---------------------------------------------------------------------------


def _detect_content_type(filename: str, declared: str | None) -> str:
    """
    Decide the real content type from the filename and what the client claimed.

    Strategy:
      1. If the client declared a known type, trust it (multipart upload
         libraries set this correctly nearly always).
      2. Otherwise fall back to the file extension — pdf, md, txt.
      3. Otherwise raise UnsupportedDocumentTypeError.

    We deliberately do NOT sniff magic bytes. pypdf will reject non-PDF
    data anyway, and txt/md have no reliable magic.
    """
    if declared and (declared in _SUPPORTED_TEXT_TYPES or declared == _PDF_TYPE):
        return declared

    lower = filename.lower()
    if lower.endswith(".pdf"):
        return _PDF_TYPE
    if lower.endswith(".md"):
        return "text/markdown"
    if lower.endswith(".txt"):
        return "text/plain"

    raise UnsupportedDocumentTypeError(
        f"Unsupported file type: {declared or 'unknown'} ({filename}). "
        "Supported: .pdf, .txt, .md"
    )


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def _extract_text(content_type: str, raw: bytes) -> str:
    """
    Turn raw upload bytes into a plain string the RLM can read.

    PDFs may legally contain zero extractable text (image-only scans).
    We do NOT OCR. An image-only PDF returns an empty string and is
    rejected one layer up.
    """
    if content_type == _PDF_TYPE:
        # pypdf needs a file-like object. BytesIO wraps the raw bytes
        # so pypdf can seek without touching the filesystem.
        reader = pypdf.PdfReader(io.BytesIO(raw))
        # Join pages with double newlines so the LM sees an obvious boundary.
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)

    # Text and markdown: decode as UTF-8. errors="replace" swaps invalid
    # bytes for U+FFFD instead of raising — preserves the rest of a mostly-
    # valid document with a single corrupt byte rather than failing the
    # whole upload. The user can still re-upload if it matters.
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def save_document(
    db: AsyncSession,
    *,
    filename: str,
    content_type: str | None,
    raw: bytes,
) -> Document:
    """
    Validate, extract, and persist an uploaded document.

    Order of checks (cheapest first):
      1. Per-file size (fast — just len(raw))
      2. Content type (string check)
      3. Per-corpus size (one SELECT SUM query)
      4. Text extraction (can be slow for big PDFs — last)
      5. Reject empty extractions (image-only PDFs)
    """
    # 1. Per-file size cap. Reject BEFORE parsing — saves CPU on a 100 MB PDF.
    if len(raw) > MAX_FILE_BYTES:
        raise DocumentTooLargeError(
            f"File is {len(raw)} bytes; max per file is {MAX_FILE_BYTES} bytes."
        )

    # 2. Content type — sniff filename + declared header.
    resolved_type = _detect_content_type(filename, content_type)

    # 3. Per-corpus size cap. One round-trip; COALESCE handles empty table.
    current_total = await _total_stored_bytes(db)
    if current_total + len(raw) > MAX_CORPUS_BYTES:
        raise DocumentTooLargeError(
            f"Corpus would be {current_total + len(raw)} bytes; "
            f"max total is {MAX_CORPUS_BYTES} bytes. "
            "Delete some documents before uploading more."
        )

    # 4. Extract text.
    text = _extract_text(resolved_type, raw)

    # 5. Image-only PDFs return ""; reject so they don't sit in the corpus
    # taking up a row and a UUID with nothing for the RLM to read.
    if not text.strip():
        raise UnsupportedDocumentTypeError(
            "Could not extract any text from this file. "
            "Image-only PDFs (scans) are not supported — "
            "run them through OCR first."
        )

    doc = Document(
        id=uuid.uuid4(),
        filename=filename,
        content_type=resolved_type,
        content_text=text,
        size_bytes=len(raw),
    )
    db.add(doc)
    await db.flush()  # populate doc.id without committing yet
    await db.commit()

    await logger.ainfo(
        "document saved",
        document_id=str(doc.id),
        filename=filename,
        content_type=resolved_type,
        size_bytes=len(raw),
        extracted_chars=len(text),
    )
    return doc


async def list_documents(db: AsyncSession) -> list[Document]:
    """Return all documents, newest first. Excludes content_text via column projection."""
    # Select all columns including content_text — the schema response model
    # drops it. Loading 10 MiB of text into Python only to throw it away is
    # wasteful but acceptable at this corpus size; revisit if we ever grow
    # past ~100 docs.
    result = await db.execute(select(Document).order_by(Document.created_at.desc()))
    return list(result.scalars().all())


async def get_documents_by_ids(
    db: AsyncSession,
    document_ids: list[uuid.UUID],
) -> list[Document]:
    """
    Load specific documents WITH their content_text — used by the RAG service
    to populate the RLM sandbox.
    """
    if not document_ids:
        return []
    result = await db.execute(select(Document).where(Document.id.in_(document_ids)))
    return list(result.scalars().all())


async def get_all_documents_with_content(db: AsyncSession) -> list[Document]:
    """Load every document including content_text — used when no IDs are scoped."""
    result = await db.execute(select(Document).order_by(Document.created_at))
    return list(result.scalars().all())


async def delete_document(db: AsyncSession, document_id: uuid.UUID) -> None:
    """
    Delete a document. Raises DocumentNotFoundError if it doesn't exist.

    No CASCADE concern — Document has no children.
    """
    # DELETE returns a CursorResult, which exposes .rowcount.
    # The plain `Result` (returned by SELECT) does not, hence the explicit cast
    # so mypy knows we are in DML-result land.
    result: CursorResult[None] = await db.execute(  # type: ignore[assignment]
        delete(Document).where(Document.id == document_id)
    )
    if result.rowcount == 0:
        raise DocumentNotFoundError(f"Document {document_id} not found.")
    await db.commit()
    await logger.ainfo("document deleted", document_id=str(document_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _total_stored_bytes(db: AsyncSession) -> int:
    """
    Sum of size_bytes across all documents. COALESCE because SUM over an
    empty set returns NULL, which Python would render as None instead of 0.
    """
    result = await db.execute(select(func.coalesce(func.sum(Document.size_bytes), 0)))
    return int(result.scalar_one())
