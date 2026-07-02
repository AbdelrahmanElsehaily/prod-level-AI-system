"""
ui/pages/3_Document_QA.py — ask questions of uploaded documents
================================================================
Calls POST /chat/docs (backed by dspy.RLM). One question → one answer.
No conversation history — each query is standalone (matches the backend
contract, which is stateless for this endpoint).

Flow:
    1. Page loads → fetch list of documents
    2. User picks which docs to include (multi-select, defaults to ALL)
    3. User types a question
    4. We POST → spinner → render answer + iteration count + token usage
"""

import lib  # noqa: E402  (Streamlit-managed sys.path; standard multipage pattern)
import streamlit as st

# ---------------------------------------------------------------------------
# Page chrome
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Document Q&A", page_icon="🔎", layout="centered")
lib.render_sidebar_health()

st.title("🔎 Document Q&A")
st.caption(
    "Powered by `dspy.RLM` — the LLM recursively explores the selected "
    "documents in a Pyodide sandbox. Capped at 20 sub-calls per question."
)

# ---------------------------------------------------------------------------
# Fetch the list of available docs (the multi-select needs them)
# ---------------------------------------------------------------------------
# We refetch on every rerun rather than cache aggressively. Documents are
# small to list (no content_text) and a user who just deleted/uploaded one
# on page 2 expects to see that change here immediately. st.cache_data with
# a short TTL would also work; the simpler thing is to just fetch.

try:
    docs = lib.get("/documents")
except lib.ApiError as exc:
    st.error(f"Failed to load document list: {exc.detail}")
    st.stop()

assert isinstance(docs, list)

if not docs:
    # Nothing to query → short-circuit with a pointer to page 2. Avoids
    # showing a useless empty selector and a question box.
    st.info("No documents available. Upload one on the **Documents** page first.")
    st.stop()

# ---------------------------------------------------------------------------
# Document selector
# ---------------------------------------------------------------------------
# Multi-select with default = ALL docs. Matches the backend contract: an
# omitted document_ids field means "use everything". We achieve the same
# behaviour explicitly here — the user always SEES what's in scope.

# Build a label→id lookup so the multiselect can show friendly names while
# we send UUIDs to the backend. Including a short id-suffix prevents
# ambiguity when two files share the same name ("notes.txt").
id_by_label: dict[str, str] = {
    f"{doc['filename']}  ({doc['id'][:8]}…)": doc["id"] for doc in docs
}
all_labels = list(id_by_label.keys())

selected_labels = st.multiselect(
    "Documents to search",
    options=all_labels,
    default=all_labels,  # ALL selected by default (Q3 option (a))
    help=(
        "All documents are selected by default. Narrow this down to focus "
        "the answer on specific files (faster and cheaper)."
    ),
)
selected_ids = [id_by_label[lbl] for lbl in selected_labels]

# Empty selection is a likely user mistake — surface it before they ask
# a question that would 404 server-side.
if not selected_ids:
    st.warning("Select at least one document.")
    st.stop()

# ---------------------------------------------------------------------------
# Question box
# ---------------------------------------------------------------------------
# st.form so the API call only fires on submit, not on every keystroke
# in the text area. Big text_area instead of chat_input — long questions
# / multi-paragraph context paste better.

with st.form("qa_form", clear_on_submit=False):
    question = st.text_area(
        "Your question",
        placeholder="e.g. What does the contract say about termination?",
        height=120,
    )
    submitted = st.form_submit_button("Ask", type="primary")

if not submitted:
    st.stop()

if not question.strip():
    st.warning("Type a question first.")
    st.stop()

# ---------------------------------------------------------------------------
# Call the backend
# ---------------------------------------------------------------------------

with st.spinner("RLM is exploring the documents…"):
    try:
        result = lib.post(
            "/chat/docs",
            json={
                "question": question,
                "document_ids": selected_ids,
            },
            # RLM can run for a while — recursive sub-calls add up. The
            # backend caps iteration count, but we still want a generous
            # client-side budget.
            timeout=180,
        )
    except lib.ApiError as exc:
        st.error(f"❌ {exc.status_code}: {exc.detail}")
        st.stop()

assert isinstance(result, dict)

# ---------------------------------------------------------------------------
# Render the answer
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Answer")
st.markdown(result["answer"])

# Diagnostics: how many sub-calls, how many tokens, which docs were in scope.
# Useful for understanding cost when the model takes "too long" / "too short".
col1, col2, col3 = st.columns(3)
col1.metric("Iterations", result.get("iterations", 0))
col2.metric("Tokens used", result.get("total_tokens", 0))
col3.metric("Docs in scope", len(result.get("documents_used", [])))

# Surface which docs the RLM was given (not necessarily which it actually
# read — RLM may or may not touch every file in the sandbox).
with st.expander("Documents the RLM had access to"):
    used_ids = set(result.get("documents_used", []))
    for doc in docs:
        if doc["id"] in used_ids:
            st.markdown(f"- `{doc['filename']}`")
