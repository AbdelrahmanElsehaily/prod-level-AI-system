"""
ui/chat_app.py — Streamlit chat UI (entry page)
================================================
Run locally:
    streamlit run ui/chat_app.py

This is the multi-page app's entry script. Streamlit auto-discovers any
`.py` file under `ui/pages/` and adds it to the sidebar nav. This file
is the "home" page → plain conversational chat against POST /chat.

Sibling pages (added by Step 13):
    pages/2_Documents.py    upload / list / delete documents
    pages/3_Document_QA.py  ask questions of those documents via dspy.RLM

Shared concerns (API base URL, error handling, health badge) live in lib.py.
This file deliberately contains no `requests.*` calls or env-var reads —
it talks to the backend only through `lib`.
"""

# NOTE: when Streamlit runs `ui/chat_app.py`, it adds the script's directory
# (`ui/`) to sys.path automatically. Sibling pages in `ui/pages/` inherit the
# same sys.path, so `import lib` resolves from any page without packaging.
import lib  # noqa: E402  (Streamlit-managed sys.path; standard multipage pattern)
import streamlit as st

# ---------------------------------------------------------------------------
# Page chrome
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Chat",
    page_icon="💬",
    layout="centered",
)

lib.render_sidebar_health()


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
# Streamlit reruns the script top-to-bottom on every interaction. Anything
# stored in st.session_state survives those reruns; locals do not.

if "messages" not in st.session_state:
    st.session_state.messages = []  # list of {role, content, meta}

if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = None


# ---------------------------------------------------------------------------
# Sidebar — chat-specific controls
# ---------------------------------------------------------------------------
# render_sidebar_health() already added the API badge; we append conversation
# state and a reset button below it.

with st.sidebar:
    if st.session_state.conversation_id:
        st.markdown("**Conversation ID**")
        st.code(st.session_state.conversation_id, language=None)

    if st.button("🗑️ New conversation", use_container_width=True):
        st.session_state.messages = []
        st.session_state.conversation_id = None
        st.rerun()


# ---------------------------------------------------------------------------
# Main chat area
# ---------------------------------------------------------------------------

st.title("💬 Chat")
st.caption("Plain chat against POST /chat. For docs Q&A, see the sidebar nav.")

# Replay history each rerun (Streamlit has no diff-render — paint everything).
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant" and msg.get("meta"):
            meta = msg["meta"]
            # cache_hit is True when the backend served from Redis cache —
            # surfacing it in the UI is a great way to *see* the cache work.
            cache_badge = " · ⚡ cached" if meta.get("cache_hit") else ""
            st.caption(
                f"🔧 {meta.get('model', '—')} · "
                f"🪙 {meta.get('tokens_used', '—')} tokens"
                f"{cache_badge}"
            )

# st.chat_input blocks here until the user submits text.
user_input = st.chat_input("Type a message…")

if user_input:
    # Echo the user message immediately so the UI feels responsive even
    # while we're waiting on the backend.
    with st.chat_message("user"):
        st.markdown(user_input)
    st.session_state.messages.append(
        {"role": "user", "content": user_input, "meta": {}}
    )

    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            try:
                data = lib.post(
                    "/chat",
                    json={
                        "message": user_input,
                        "conversation_id": st.session_state.conversation_id,
                    },
                )
            except lib.ApiError as exc:
                st.error(f"❌ {exc.status_code or 'Error'}: {exc.detail}")
            else:
                assert isinstance(data, dict)  # narrow for type checkers
                reply = data["reply"]
                st.session_state.conversation_id = data["conversation_id"]
                meta = {
                    "model": data.get("model"),
                    "tokens_used": data.get("tokens_used"),
                    "cache_hit": data.get("cache_hit", False),
                }
                st.markdown(reply)
                cache_badge = " · ⚡ cached" if meta["cache_hit"] else ""
                st.caption(
                    f"🔧 {meta['model']} · 🪙 {meta['tokens_used']} tokens{cache_badge}"
                )
                st.session_state.messages.append(
                    {"role": "assistant", "content": reply, "meta": meta}
                )
