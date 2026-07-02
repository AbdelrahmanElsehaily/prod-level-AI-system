"""
ui/pages/2_Documents.py — upload, list, delete documents
=========================================================
Calls the backend's POST/GET/DELETE /documents endpoints. Streamlit shows
this in the sidebar nav as "Documents" (filename prefix `2_` controls
the order; emoji comes from page_icon below).

Run via the entry script:
    streamlit run ui/chat_app.py

Why this page is CRUD only, no chat:
  Each Streamlit page does ONE thing. Mixing upload + Q&A on the same
  page muddles the user mental model and tangles session state. Page 3
  (Document_QA) is where the actual questions happen.
"""

from datetime import datetime

import lib  # noqa: E402  (Streamlit-managed sys.path; standard multipage pattern)
import streamlit as st

# ---------------------------------------------------------------------------
# Page chrome
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Documents", page_icon="📄", layout="centered")
lib.render_sidebar_health()

st.title("📄 Documents")
st.caption(
    "Upload PDFs, .txt, or .md files. The backend extracts text and stores it; "
    "the original binary is discarded. Use page 3 to ask questions."
)


# ---------------------------------------------------------------------------
# Helpers (defined before use — Streamlit runs the whole script top-to-bottom)
# ---------------------------------------------------------------------------


def _relative_age(iso_ts: str) -> str:
    """
    Render a timestamp as "3m ago" / "2d ago".

    Approximate — anything older than a day rolls up to "Nd ago" instead of
    distinguishing weeks/months/years. Good enough for an upload list where
    the user just wants to know "is this recent".
    """
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return iso_ts

    delta = datetime.now(ts.tzinfo) - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


# ---------------------------------------------------------------------------
# Upload form
# ---------------------------------------------------------------------------
# st.form batches widget changes: nothing happens on every keystroke; we
# re-run only when the submit button fires. Keeps uploads predictable and
# avoids accidental re-uploads on Streamlit's auto-rerun behaviour.

with st.form("upload_form", clear_on_submit=True):
    uploaded = st.file_uploader(
        "Choose a file",
        type=["pdf", "txt", "md"],
        accept_multiple_files=False,
        help="Max 1 MiB per file. Corpus total capped at 10 MiB.",
    )
    submitted = st.form_submit_button("Upload", type="primary")

    if submitted:
        if uploaded is None:
            st.warning("Select a file first.")
        else:
            with st.spinner("Uploading…"):
                try:
                    # `files=` is requests' multipart shape. Tuple = (name,
                    # bytes, content_type) — the backend uses all three.
                    result = lib.post(
                        "/documents",
                        files={
                            "file": (
                                uploaded.name,
                                uploaded.getvalue(),
                                uploaded.type,
                            )
                        },
                        timeout=lib.TIMEOUT_UPLOAD,
                    )
                except lib.ApiError as exc:
                    # 413 = too large, 415 = unsupported type, others as-is.
                    # Show the backend's detail so users see WHY it failed
                    # (e.g. "Could not extract any text from this file").
                    st.error(f"❌ {exc.status_code}: {exc.detail}")
                else:
                    assert isinstance(result, dict)
                    st.success(f"Uploaded `{result['filename']}`")

# ---------------------------------------------------------------------------
# Existing documents list
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Stored documents")

try:
    docs = lib.get("/documents")
except lib.ApiError as exc:
    st.error(f"Failed to load documents: {exc.detail}")
    docs = []

assert isinstance(docs, list)

if not docs:
    st.info("No documents uploaded yet. Add one above.")
else:
    # st.dataframe has no per-row buttons, so we render each doc as a 4-col
    # layout. Canonical Streamlit pattern for "table with row actions".
    for doc in docs:
        cols = st.columns([4, 2, 2, 1])
        cols[0].markdown(f"**{doc['filename']}**")
        cols[1].caption(doc["content_type"])
        kib = doc["size_bytes"] / 1024
        cols[2].caption(f"{kib:.1f} KiB · {_relative_age(doc['created_at'])}")
        # key= must be unique per widget. Using the doc id guarantees that
        # even after a re-render the right button maps to the right doc.
        if cols[3].button("🗑️", key=f"del_{doc['id']}", help="Delete"):
            try:
                lib.delete(f"/documents/{doc['id']}")
            except lib.ApiError as exc:
                st.error(f"Delete failed: {exc.detail}")
            else:
                # rerun() so the deleted row vanishes immediately without
                # the user having to click anything else.
                st.rerun()
