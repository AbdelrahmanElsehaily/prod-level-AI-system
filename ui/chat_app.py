"""
ui/chat_app.py — Streamlit chat UI for the Chat API
=====================================================
Run locally:
    streamlit run ui/chat_app.py

Environment variables:
    CHAT_API_URL  — base URL of the FastAPI backend (default: http://localhost:8000)

This UI is intentionally simple — it is a demo interface for internal use,
not a production frontend. It calls POST /chat on the backend and displays
the conversation using Streamlit's native chat components.
"""

import os

import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_URL = os.getenv("CHAT_API_URL", "http://localhost:8000")
CHAT_ENDPOINT = f"{API_URL}/chat"
HEALTH_ENDPOINT = f"{API_URL}/health"

st.set_page_config(
    page_title="Chat",
    page_icon="💬",
    layout="centered",
)

# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------
# Streamlit reruns the entire script on every interaction. session_state
# persists values across reruns so the conversation isn't lost.

if "messages" not in st.session_state:
    st.session_state.messages = []  # list of {"role": str, "content": str, "meta": dict}

if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = None

# ---------------------------------------------------------------------------
# Sidebar — connection info + controls
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("⚙️ Settings")
    st.markdown(f"**API:** `{API_URL}`")

    # Live health check
    try:
        resp = requests.get(HEALTH_ENDPOINT, timeout=3)
        health = resp.json()
        if resp.status_code == 200:
            st.success("API healthy ✓")
        else:
            st.warning(f"API degraded — {health.get('checks', {})}")
    except requests.exceptions.RequestException:
        st.error("API unreachable ✗")

    st.divider()

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

# Render existing messages
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant" and msg.get("meta"):
            meta = msg["meta"]
            st.caption(
                f"🔧 {meta.get('model', '—')} · "
                f"🪙 {meta.get('tokens_used', '—')} tokens"
            )

# Chat input — Streamlit blocks here until the user submits
user_input = st.chat_input("Type a message…")

if user_input:
    # Show user message immediately
    with st.chat_message("user"):
        st.markdown(user_input)
    st.session_state.messages.append({"role": "user", "content": user_input, "meta": {}})

    # Call the API
    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            try:
                payload = {
                    "message": user_input,
                    "conversation_id": st.session_state.conversation_id,
                }
                response = requests.post(CHAT_ENDPOINT, json=payload, timeout=60)
                response.raise_for_status()
                data = response.json()

                reply = data["reply"]
                st.session_state.conversation_id = data["conversation_id"]
                meta = {
                    "model": data.get("model"),
                    "tokens_used": data.get("tokens_used"),
                }

                st.markdown(reply)
                st.caption(
                    f"🔧 {meta['model']} · 🪙 {meta['tokens_used']} tokens"
                )

                st.session_state.messages.append(
                    {"role": "assistant", "content": reply, "meta": meta}
                )

            except requests.exceptions.Timeout:
                st.error("⏱️ The API took too long to respond. Try again.")
            except requests.exceptions.ConnectionError:
                st.error(f"🔌 Could not reach the API at `{API_URL}`. Is it running?")
            except requests.exceptions.HTTPError as e:
                detail = ""
                try:
                    detail = e.response.json().get("detail", "")
                except Exception:
                    pass
                st.error(f"❌ API error {e.response.status_code}: {detail or str(e)}")
